/**
 * GARDE-FOU PROD-SAFE — enforcement au niveau RÉSEAU.
 *
 * ── Pourquoi ce module existe ────────────────────────────────────────────────
 * Le dashboard de dev pilote la ferme RDS de PRODUCTION. La protection reposait
 * jusqu'ici sur une denylist regex appliquée à l'attribut `onclick` et au texte
 * visible des boutons. Elle est structurellement insuffisante :
 *
 *   - un bouton câblé par `addEventListener` n'a AUCUN attribut `onclick` à tester ;
 *   - un bouton dont le libellé est une icône n'a aucun texte à tester ;
 *   - les motifs ne couvraient pas les noms réels des actions
 *     (`clear_temp` ne contient pas « clean », `flush_dns` ne contient pas « purge »).
 *
 * Conséquence constatée le 2026-07-30 : un run de la gate a réellement exécuté
 * `restart_spooler` ×2, `optimize_ram` ×3, `clear_temp` (17,7 Mo supprimés) et
 * `flush_dns` ×2 sur l'hôte broker — en production, pendant les heures ouvrées.
 *
 * ── Principe ─────────────────────────────────────────────────────────────────
 * La sûreté ne doit pas dépendre de ce qu'on croit deviner d'un bouton, mais de
 * ce qui SORT réellement sur le réseau. On intercepte donc chaque requête et on
 * applique une ALLOWLIST : tout ce qui n'est pas explicitement reconnu comme non
 * intrusif est bloqué et remplacé par une réponse 200 factice — l'UI se comporte
 * normalement, donc les bugs d'affichage restent détectables, mais rien n'atteint
 * la ferme.
 *
 * Allowlist, et pas denylist : une action ajoutée demain sera bloquée par défaut.
 * C'est l'inverse qui a causé l'incident.
 */

// Actions agent réellement non intrusives : elles LISENT l'état de l'hôte.
// Volontairement ABSENTS de cette liste :
//   - shadow / screenshot : techniquement non mutants, mais ils prennent la main
//     sur la session d'un utilisateur ou capturent son écran. Sur une ferme de
//     production avec de vrais utilisateurs, ça ne s'automatise pas.
//   - manage_profiles : fait de la réparation d'ACL, donc mutant malgré son nom.
const READ_ONLY_ACTIONS = new Set([
  'get_profiles',
  'get_profile_size',
  'analyze_profile',
  'diagnose_rds',
  'audit_security',
  'manage_apps',
]);

// Noms d'outils UI (clés de TOOL_MAP dans Web/routes/actions.py) autorisés
const READ_ONLY_TOOLS = new Set([
  'troubleshootinganddiagnostics.ps1',  // → diagnose_rds
  'securityandcompliance.ps1',          // → audit_security
  'applicationmanagement.ps1',          // → manage_apps
]);

// Endpoints bloqués sans condition, quel que soit le contenu du corps
const ALWAYS_BLOCK = [
  '/api/shell',                 // exécution PowerShell sur le broker
  '/api/admin/',                // agent-action, fleet-update, master-action
  '/api/licenses/repair',       // purge de jeton Office sur un hôte réel
  // Lecture seule CÔTÉ EFFET, mais met quand même une commande en file sur un
  // hôte de production : un run e2e enverrait un diagnostic à chaque clic.
  '/api/licenses/diagnose',
  '/api/master-config-reload',
  // 🔴 Envoie un VRAI mail aux destinataires configurés. Un POST /api/* non
  // catalogué passe par défaut (voir le retour `unknown` de decide()), et la QA
  // clique tous les boutons : sans cette entrée, chaque run spammerait support@.
  '/api/alerts/test',
];

// Identifiant de commande factice rendu par le garde-fou. Le poll de statut
// correspondant est intercepté lui aussi, sinon l'UI tourne jusqu'au timeout
// et affiche une erreur — un faux positif qui noierait les vrais défauts.
const FAKE_CMD_ID = 'qa-blocked';

/**
 * Réponse factice CONFORME au contrat de chaque endpoint.
 *
 * Renvoyer une forme générique ne suffit pas : chaque route a son propre schéma
 * (`output` pour /api/tools, `commandId` + poll pour /api/actions, `message` pour
 * /api/profile-action). Une réponse mal formée fait afficher un toast d'erreur à
 * l'UI, et l'audit compte alors comme « défaut » ce que le garde-fou vient de
 * bloquer. Constaté : 153 faux positifs sur un run.
 */
function fakeResponseFor(path) {
  const note = '[QA prod-safe] action bloquée avant la ferme de production';

  if (path.startsWith('/api/tools')) {
    return { status: 'success', output: note };
  }
  if (path.startsWith('/api/shell')) {
    return { status: 'success', output: note };
  }
  if (path.startsWith('/api/actions')) {
    return { status: 'success', commandId: FAKE_CMD_ID, message: note };
  }
  if (path.startsWith('/api/profile-action')) {
    return { status: 'success', commandId: FAKE_CMD_ID, message: note };
  }
  // Contrat REEL de cette route : {status:'queued', command:{id}} — et surtout PAS
  // `commandId` comme les autres. Avec la forme generique, licenses.html tombait
  // dans sa branche d'erreur et affichait « Erreur : {...} » : le garde-fou faisait
  // passer une page saine pour cassee. Verifie le 31/07/2026.
  if (path.startsWith('/api/licenses/repair') || path.startsWith('/api/licenses/diagnose')) {
    return { status: 'queued', command: { id: FAKE_CMD_ID }, message: note };
  }
  if (path.startsWith('/api/admin/')) {
    return { status: 'success', message: note, output: note, result: note };
  }
  // Contrat réel : {status:'success', sent:bool}. Une forme générique ferait
  // tomber la page dans sa branche d'erreur et compterait un défaut là où le
  // garde-fou vient de faire son travail (leçon /api/licenses/repair, 31/07).
  if (path.startsWith('/api/alerts/test')) {
    return { status: 'success', sent: false, message: note };
  }
  return { status: 'success', message: note, output: note };
}

function decide(url, method, body) {
  const path0 = url.replace(/^https?:\/\/[^/]+/, '');

  // Poll de statut de la commande factice : répondre « terminée avec succès »
  if (method === 'GET' && path0.includes(`/api/command-status/${FAKE_CMD_ID}`)) {
    return { blocked: true, fakeStatus: true, why: 'poll de la commande factice' };
  }

  if (method !== 'POST') return { blocked: false };

  const path = path0;
  if (!path.startsWith('/api/')) return { blocked: false };

  for (const prefix of ALWAYS_BLOCK) {
    if (path.startsWith(prefix)) return { blocked: true, why: `endpoint ${prefix}` };
  }

  let data = {};
  try { data = body ? JSON.parse(body) : {}; } catch (_) { /* corps non JSON */ }

  // /api/actions et /api/profile-action portent le nom de l'action
  if (path.startsWith('/api/actions') || path.startsWith('/api/profile-action')) {
    const action = String(data.action || '').toLowerCase();

    // `clean_downloads` en SIMULATION est une lecture : l'agent n'entre dans sa
    // branche de suppression que si `-not $DryRun` (Invoke-DownloadsCleanup).
    // On l'autorise donc UNIQUEMENT avec dry_run === true explicite — jamais par
    // défaut, jamais si la clé est absente.
    //
    // Pourquoi cet élargissement, alors que l'Iron Law 1 l'interdit sans preuve :
    // parce que son absence a laissé passer un bug de PRODUCTION. Les boutons
    // SIMULER/NETTOYER/SCANNER envoyaient `target:` là où l'API lit `server:` —
    // ils n'ont jamais rien déclenché depuis le 31/07. Aucun test ne l'a vu : la
    // QA n'atteignait pas le bouton, et les vérifications manuelles passaient par
    // l'AgentMaster en direct, donc CONTOURNAIENT la route fautive. Un garde-fou
    // qui empêche de tester le chemin réel finit par protéger le défaut.
    const params = data.params || {};
    if (action === 'clean_downloads' && params.dry_run === true) {
      return { blocked: false, simulation: true };
    }
    // /api/profile-action traduit : analyze→analyze_profile, size→get_profile_size, clean→clean_profile
    const normalized = { analyze: 'analyze_profile', size: 'get_profile_size', clean: 'clean_profile' }[action] || action;
    if (READ_ONLY_ACTIONS.has(normalized)) return { blocked: false };
    return { blocked: true, action: normalized, why: `action '${action || '(vide)'}'` };
  }

  // /api/tools porte un nom d'outil UI
  if (path.startsWith('/api/tools')) {
    const tool = String(data.script || '').toLowerCase();
    if (READ_ONLY_TOOLS.has(tool)) return { blocked: false };
    return { blocked: true, action: tool, why: `outil '${tool || '(vide)'}'` };
  }

  // Rafraîchissements catalogués comme lecture : ils demandent un scan à l'agent,
  // mais ne modifient rien sur l'hôte.
  if (path.startsWith('/api/profiles-disk-info-refresh')) return { blocked: false };

  // Interroge Mailjet sur le sort de messages DÉJÀ envoyés : n'expédie rien,
  // ne touche aucun hôte. Catalogué explicitement pour ne pas rester dans le
  // fourre-tout « POST non catalogué » — ce compteur doit signaler les vraies
  // nouveautés, pas s'habituer à du bruit connu.
  if (path.startsWith('/api/alerts/refresh-delivery')) return { blocked: false };

  // Tout autre POST /api/* non identifié : on laisse passer (rafraîchissements,
  // recherches…) mais on le trace, pour que l'ajout d'un endpoint mutant se voie.
  return { blocked: false, unknown: true, why: `POST non catalogué ${path}` };
}

/**
 * Arme le garde-fou sur une page Playwright.
 * Retourne un objet de suivi : { blocked: [...], unknown: [...], summary() }
 */
// PNG 1×1 transparent. `sessions.html` fait `img.src = d.result` sur le résultat
// d'un screenshot/shadow : rendre un texte à la place produirait une requête vers
// une URL absurde et un 404 compté comme défaut (24 faux positifs observés).
const BLANK_PNG =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

async function armProdSafe(page, { verbose = true } = {}) {
  const blocked = [];
  const unknown = [];
  let lastBlockedAction = null;   // façonne la réponse du poll de statut

  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const verdict = decide(req.url(), req.method(), req.postData());

    if (verdict.blocked) {
      const path = req.url().replace(/^https?:\/\/[^/]+/, '');

      // Poll du statut de la commande factice → « terminée, succès »
      if (verdict.fakeStatus) {
        // screenshot / shadow : le résultat est injecté tel quel dans un <img src>
        const isImage = lastBlockedAction === 'screenshot' || lastBlockedAction === 'shadow';
        const result = isImage
          ? BLANK_PNG
          : '[QA prod-safe] action bloquée avant la ferme de production';
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            id: 'qa-blocked',
            status: 'success',
            result,
            result_message: result,
            timestamp: new Date().toISOString(),
          }),
        });
      }

      lastBlockedAction = verdict.action || null;
      blocked.push(verdict.why);
      if (verbose) console.log(`     🛡️  BLOQUÉ (prod-safe) : ${verdict.why}`);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(fakeResponseFor(path)),
      });
    }

    if (verdict.unknown) unknown.push(verdict.why);
    return route.continue();
  });

  return {
    blocked,
    unknown,
    summary() {
      const lines = [];
      if (blocked.length) lines.push(`🛡️  ${blocked.length} requête(s) mutante(s) bloquée(s) avant la prod`);
      if (unknown.length) {
        const uniq = [...new Set(unknown)];
        lines.push(`ℹ️  ${unknown.length} POST non catalogué(s) laissé(s) passer : ${uniq.slice(0, 5).join(', ')}`);
      }
      return lines;
    },
  };
}

module.exports = { armProdSafe, READ_ONLY_ACTIONS, READ_ONLY_TOOLS, decide };
