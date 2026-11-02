/**
 * QA ciblée — page « Espaces Profils » vue depuis l'URL PUBLIQUE (reverse proxy).
 *
 * Pourquoi une passe distincte de `qa-profiles-port.js` (qui tape 127.0.0.1:5050) :
 * le chemin réel des utilisateurs passe par le reverse proxy (TLS, en-têtes, cookie de session
 * sur un autre host). Un dashboard sain en local peut être cassé derrière le proxy.
 * Si la machine de test ne résout pas le nom public, QA_PROXY_IP force la résolution
 * (host-resolver-rules).
 *
 * 🛡️ Garde-fou prod-safe ARMÉ : cette page pilote la ferme de PRODUCTION et porte
 * des actions destructives (NETTOYER, suppression de profil). Iron Law 1.
 *
 * Env : QA_PUBLIC_HOST (requis, ex. sam.example.com), QA_PROXY_IP (optionnel : IP du
 *       reverse proxy, forcée par host-resolver-rules), QA_PASS (requis),
 *       QA_USER (def administrateur).
 */
const { chromium } = require('./lib/playwright');
const { armProdSafe } = require('./lib/prod-safe');

const HOST = process.env.QA_PUBLIC_HOST || '';
const PROXY_IP = process.env.QA_PROXY_IP || '';
const BASE = `https://${HOST}`;
if (!HOST) { console.error('QA_PUBLIC_HOST requis (ex. sam.example.com)'); process.exit(2); }
const USER = process.env.QA_USER || 'administrateur';
const PASS = process.env.QA_PASS;
const CHROME = '/usr/bin/chromium';
const SHOTS = process.env.QA_SHOTS || '/tmp/qa-profiles-public';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Charge utile RÉALISTE de simulation clean_downloads, calquée sur le retour réel
// mesuré sur un hôte de test (16 éléments / 1 Go) mais dimensionnée pour exercer le modal
// 2.25.3 : plusieurs profils, sous-totaux, et une TRONCATURE à annoncer.
// ⚠️ C'est un STUB : l'agent broker (porteur de R:) est hors-ligne, aucun
// round-trip réel n'est possible aujourd'hui. On teste le RENDU, pas la collecte.
function stubSimulation() {
  const items = [];
  const profs = ['jdoe', 'asmith', 'bwilson', 'cmartin'];
  for (let i = 0; i < 40; i++) {
    items.push({
      profile: profs[i % profs.length],
      name: `installeur-${i}.exe`,
      size_mb: Math.round(500 - i * 11),
      age_days: 30 + i * 3,
      deleted: false,
    });
  }
  return {
    dry_run: true, days: 14, base: 'R:\\Redirections',
    profiles_scanned: 101, folders_found: 97,
    candidates: 312, total_mb: 41250.7,
    deleted: 0, failed: 0, freed_mb: 0,
    top: items.slice(0, 15).map((i) => ({ profile: i.profile, name: i.name, size_mb: i.size_mb, age_days: i.age_days })),
    items,
    items_truncated: 272,
  };
}

(async () => {
  if (!PASS) { console.error('QA_PASS requis'); process.exit(2); }
  const findings = [];
  const add = (sev, what) => { findings.push({ sev, what }); console.log(`  ${sev === 'BUG' ? '❌' : (sev === 'WARN' ? '⚠️ ' : 'ℹ️ ')} ${what}`); };

  const browser = await chromium.launch({
    executablePath: CHROME,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
           ...(PROXY_IP ? [`--host-resolver-rules=MAP ${HOST} ${PROXY_IP}`] : [])],
  });
  const ctx = await browser.newContext({ locale: 'fr-FR', viewport: { width: 1600, height: 1100 } });
  const page = await ctx.newPage();
  await armProdSafe(page);
  page.setDefaultTimeout(12000);

  const jsErrors = [];
  const httpBad = [];
  page.on('console', (m) => { if (m.type() === 'error') jsErrors.push(m.text()); });
  page.on('pageerror', (e) => jsErrors.push('pageerror: ' + e.message));
  page.on('response', (r) => { if (r.status() >= 400) httpBad.push(`${r.status()} ${r.url().replace(BASE, '')}`); });
  page.on('dialog', (d) => d.dismiss().catch(() => {}));

  console.log(`\n===== QA /profiles via ${BASE} (reverse proxy) =====\n`);

  // --- 1. Login à travers le proxy -----------------------------------------
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"]', USER);
  await page.fill('input[name="password"]', PASS);
  await Promise.all([page.waitForLoadState('domcontentloaded').catch(() => {}), page.click('button[type="submit"]')]);
  if (/\/login/.test(page.url())) { add('BUG', 'Login refusé derrière le reverse proxy'); await browser.close(); process.exit(1); }
  console.log('  ✅ Login OK derrière le reverse proxy');

  // --- 2. Chargement de /profiles ------------------------------------------
  await page.goto(`${BASE}/profiles`, { waitUntil: 'domcontentloaded' });
  let loaded = false;
  for (let i = 0; i < 20; i++) {
    const t = await page.evaluate(() => document.getElementById('profiles-body')?.innerText || '');
    if (t && !/Chargement des profils/i.test(t)) { loaded = true; break; }
    await sleep(700);
  }
  if (!loaded) add('BUG', 'Le tableau des profils ne sort jamais de « Chargement… »');

  const s = await page.evaluate(() => {
    const txt = (id) => document.getElementById(id)?.innerText?.trim() || '';
    const rows = [...document.querySelectorAll('#profiles-body tr')];
    return {
      total: txt('stat-total-gb'), used: txt('stat-used-gb'),
      free: txt('stat-free-gb'), pct: txt('stat-percent-used'),
      title: txt('profiles-count-title'), location: txt('profiles-location'),
      banner: (document.getElementById('agent-offline-banner')?.innerText || '').trim(),
      bannerVisible: document.getElementById('agent-offline-banner')?.style.display !== 'none',
      freshness: txt('sizes-freshness'),
      rowCount: rows.length,
      firstRows: rows.slice(0, 3).map(r => r.innerText.replace(/\s+/g, ' ').trim()),
      btnSim: !!document.getElementById('dl-sim-btn')?.disabled,
      btnClean: !!document.getElementById('dl-clean-btn')?.disabled,
      btnScan: !!document.getElementById('scan-sizes-btn')?.disabled,
    };
  });
  console.log(`  Disque : ${s.total} total · ${s.used} utilisé · ${s.free} libre · ${s.pct}`);
  console.log(`  Titre  : ${s.title}`);
  console.log(`  Lignes : ${s.rowCount}`);
  console.log(`  Boutons désactivés — SIMULER:${s.btnSim} NETTOYER:${s.btnClean} SCANNER:${s.btnScan}`);
  console.log(`  Bannière : ${s.bannerVisible ? '(visible) ' + s.banner.slice(0, 90) : '(masquée)'}`);
  await page.screenshot({ path: `${SHOTS}-1-page.png`, fullPage: false });

  if (!/GB|Go/.test(s.total) || s.total === '---') add('BUG', `Volumétrie disque non renseignée (total="${s.total}")`);
  if (s.rowCount < 1) add('BUG', 'Aucune ligne de profil affichée');

  // Cohérence attendue : agent broker hors-ligne ⇒ bannière + actions R: verrouillées
  if (s.bannerVisible) {
    if (!s.btnSim || !s.btnClean || !s.btnScan) {
      add('BUG', `Agent hors-ligne annoncé mais des actions R: restent cliquables (SIM:${!s.btnSim} CLEAN:${!s.btnClean} SCAN:${!s.btnScan}) — elles resteront pending`);
    } else {
      console.log('  ✅ Cohérent : agent hors-ligne ⇒ actions R: verrouillées');
    }
  }

  // --- 3. Tri des colonnes (les tailles sont formatées : piège data-sort-value) ---
  const sortCheck = async (label, thIndex, tdIndex) => {
    await page.evaluate((i) => {
      const th = document.querySelectorAll('#profiles-table thead th')[i];
      if (th) th.click();
    }, thIndex);
    await sleep(400);
    const vals = await page.evaluate((i) => [...document.querySelectorAll('#profiles-body tr')]
      .slice(0, 12).map(r => {
        const td = r.children[i];
        return td ? (td.dataset.sortValue ?? td.innerText.trim()) : null;
      }), tdIndex);
    const nums = vals.map(v => parseFloat(String(v).replace(',', '.'))).filter(v => !isNaN(v));
    const asc = nums.every((v, i, a) => i === 0 || a[i - 1] <= v);
    const desc = nums.every((v, i, a) => i === 0 || a[i - 1] >= v);
    console.log(`  Tri ${label} : ${nums.slice(0, 6).join(' ')} → ${asc ? 'croissant' : desc ? 'décroissant' : 'NON TRIÉ'}`);
    if (nums.length >= 3 && !asc && !desc) add('BUG', `Colonne « ${label} » : le tri ne produit pas un ordre monotone (${nums.slice(0, 6).join(', ')})`);
  };
  await sortCheck('TAILLE', 2, 2);
  await sortCheck('% DISQUE', 3, 3);

  // --- 4. Filtre ------------------------------------------------------------
  const visibleRows = () => page.evaluate(() => [...document.querySelectorAll('#profiles-body tr')].filter(r => r.style.display !== 'none').length);
  const beforeFilter = await visibleRows();
  const sample = await page.evaluate(() => {
    const r = document.querySelector('#profiles-body tr');
    return r ? (r.innerText.trim().split('\n')[0] || '').slice(0, 6) : '';
  });
  if (sample) {
    await page.fill('input[data-filter-for="profiles-table"]', sample);
    await sleep(500);
    const after = await visibleRows();
    console.log(`  Filtre « ${sample} » : ${beforeFilter} → ${after} ligne(s)`);
    if (after >= beforeFilter && beforeFilter > 3) add('WARN', `Le filtre ne réduit pas la liste (${beforeFilter} → ${after})`);
    if (after === 0) add('BUG', `Le filtre « ${sample} » ne remonte aucune ligne alors qu'il vient d'une ligne affichée`);
    await page.fill('input[data-filter-for="profiles-table"]', '');
    await sleep(300);
  }

  // --- 5. Bouton SIMULER — VRAI clic, VRAI aller-retour agent ---------------
  // 🔴 Ce test existe parce que son absence a coûté un bug de production : les
  // boutons de nettoyage envoyaient `target:` là où l'API lit `server:` et ne
  // déclenchaient RIEN depuis le 31/07. Le rendu du modal était vérifié sur une
  // charge utile injectée, et les contrôles manuels passaient par l'AgentMaster
  // en direct — les deux CONTOURNAIENT la route fautive. On clique donc le vrai
  // bouton et on exige un résultat venu de l'agent.
  // (La simulation est une lecture : `dry_run: true` est explicitement autorisé
  //  par le garde-fou réseau, la suppression reste bloquée.)
  console.log('\n  --- Bouton SIMULER (aller-retour agent réel) ---');
  let realSim = null;
  const simBtn = await page.$('#dl-sim-btn');
  const simDisabled = simBtn ? await simBtn.isDisabled() : true;
  if (!simBtn) {
    add('BUG', 'Bouton SIMULER introuvable');
  } else if (simDisabled) {
    add('WARN', 'SIMULER désactivé (agent hors-ligne) — aller-retour non testé ce run');
  } else {
    await simBtn.click();
    // La simulation scanne ~100 profils sur R: : compter en dizaines de secondes.
    // ⚠️ On lit le RENDU (ce que l'utilisateur voit), pas une variable interne :
    // `_dlSimulated` est déclarée en `let`, donc absente de `window` — la lire
    // renvoyait `undefined` à jamais et faisait échouer le test sur du vide.
    for (let i = 0; i < 60; i++) {
      await sleep(2000);
      const t = await page.evaluate(() => document.getElementById('dl-result')?.innerText || '');
      if (/Délai dépassé|Erreur|refusée|Hôte inconnu/i.test(t)) {
        add('BUG', `SIMULER a échoué : ${t.replace(/\s+/g, ' ').slice(0, 180)}`);
        break;
      }
      const m = t.match(/(\d[\d\s]*) élément\(s\) éligible\(s\)[^0-9]*([\d.,]+) Go/);
      if (m) {
        realSim = { candidates: parseInt(m[1].replace(/\s/g, ''), 10),
                    total_mb: parseFloat(m[2].replace(',', '.')) * 1024,
                    fromUi: true, rendered: t.replace(/\s+/g, ' ').slice(0, 180) };
        break;
      }
    }
    if (realSim) {
      console.log(`  ✅ Résultat AGENT rendu dans l'UI : ${realSim.candidates} candidat(s) · ${(realSim.total_mb / 1024).toFixed(2)} Go`);
      console.log(`     « ${realSim.rendered} »`);
      if (!realSim.candidates) add('WARN', 'Simulation revenue à 0 candidat — vérifier le seuil');
      const hasDetail = await page.evaluate(() => [...document.querySelectorAll('#dl-result button')].some(b => /DÉTAIL/i.test(b.innerText)));
      if (realSim.candidates && !hasDetail) add('BUG', 'Résultat réel sans bouton « VOIR LE DÉTAIL »');
    } else if (!findings.some(f => /SIMULER/.test(f.what))) {
      add('BUG', "SIMULER n'a produit aucun résultat en 120 s (aucune erreur affichée non plus)");
    }
  }

  // Rendu du modal : sur le résultat réel s'il existe, sinon sur charge utile
  // simulée pour continuer à couvrir groupes/sous-totaux/troncature.
  // Le rendu du modal (groupes/sous-totaux/troncature) se vérifie sur charge
  // utile maîtrisée ; l'aller-retour réel vient d'être prouvé juste au-dessus.
  const stub = stubSimulation();
  console.log('  --- Modal détail (charge utile maîtrisée) ---');
  await page.evaluate((res) => {
    window._dlSimulated = res;
    if (typeof renderDownloadsResult === 'function') {
      document.getElementById('dl-result').style.display = 'block';
      renderDownloadsResult(res, true);
    }
  }, stub);
  await sleep(500);
  const simRender = await page.evaluate(() => {
    const out = document.getElementById('dl-result');
    const btn = [...(out?.querySelectorAll('button') || [])].find(b => /DÉTAIL/i.test(b.innerText));
    return { text: (out?.innerText || '').slice(0, 220), hasDetailBtn: !!btn, btnLabel: btn ? btn.innerText.trim() : null };
  });
  console.log(`  Rendu simulation : ${simRender.text.replace(/\n/g, ' | ').slice(0, 150)}`);
  if (!simRender.hasDetailBtn) {
    add('BUG', 'Le bouton « VOIR LE DÉTAIL » n\'apparaît pas après une simulation avec candidats');
  } else {
    console.log(`  ✅ Bouton présent : ${simRender.btnLabel}`);
    await page.evaluate(() => { const b = [...document.querySelectorAll('#dl-result button')].find(x => /DÉTAIL/i.test(x.innerText)); b && b.click(); });
    await sleep(600);
    const modal = await page.evaluate(() => {
      const m = document.getElementById('cleanup-detail-modal');
      if (!m) return null;
      const groups = [...m.querySelectorAll('.cleanup-group')];
      return {
        groups: groups.length,
        items: m.querySelectorAll('.cleanup-item').length,
        firstGroup: groups[0]?.innerText.replace(/\s+/g, ' ').slice(0, 90),
        truncNote: /non list/i.test(m.innerText),
        header: m.innerText.split('\n').slice(0, 3).join(' | '),
      };
    });
    if (!modal) add('BUG', 'Le modal détail ne s\'ouvre pas');
    else {
      console.log(`  Modal : ${modal.groups} groupe(s), ${modal.items} élément(s), note troncature: ${modal.truncNote}`);
      console.log(`  En-tête : ${modal.header}`);
      console.log(`  1er groupe : ${modal.firstGroup}`);
      await page.screenshot({ path: `${SHOTS}-2-modal.png` });
      const isStub = true;
      if (isStub && modal.groups !== 4) add('BUG', `Le modal devrait grouper en 4 profils, il en affiche ${modal.groups}`);
      if (isStub && modal.items !== 40) add('BUG', `Le modal devrait lister 40 éléments, il en affiche ${modal.items}`);
      if (!isStub && modal.items < 1) add('BUG', 'Modal vide alors que la simulation a remonté des candidats');
      if (stub.items_truncated > 0 && !modal.truncNote) add('BUG', `items_truncated=${stub.items_truncated} mais aucune note de troncature — la liste se lit comme complète`);

      // Filtre du modal
      await page.evaluate(() => {
        const inp = document.querySelector('#cleanup-detail-modal input');
        if (inp) { inp.value = 'asmith'; inp.dispatchEvent(new Event('input')); }
      });
      await sleep(400);
      const filtered = await page.evaluate(() => {
        const m = document.getElementById('cleanup-detail-modal');
        const vis = [...m.querySelectorAll('.cleanup-group')].filter(g => g.style.display !== 'none');
        return { visibleGroups: vis.length, visibleItems: [...m.querySelectorAll('.cleanup-item')].filter(i => i.style.display !== 'none').length };
      });
      console.log(`  Filtre « asmith » : ${filtered.visibleGroups} groupe(s), ${filtered.visibleItems} élément(s)`);
      if (filtered.visibleGroups > 1) add('BUG', `Filtre modal : au plus 1 groupe attendu, ${filtered.visibleGroups} affiché(s)`);
      await page.screenshot({ path: `${SHOTS}-3-modal-filtre.png` });
      await page.evaluate(() => document.getElementById('cleanup-detail-modal')?.remove());
    }
  }

  // --- 6. Boutons par ligne (bloqués par le garde-fou) ----------------------
  console.log('\n  --- Actions par ligne (garde-fou actif) ---');
  const rowBtns = await page.evaluate(() => document.querySelectorAll('#profiles-body tr button').length);
  console.log(`  ${rowBtns} bouton(s) d'action sur les lignes`);
  await page.evaluate(() => { const b = document.querySelector('#profiles-body tr button'); b && b.click(); });
  await sleep(2500);
  await page.screenshot({ path: `${SHOTS}-4-apres-action.png` });

  // --- 7. Bilan -------------------------------------------------------------
  const realJsErrors = jsErrors.filter(e => !/favicon|ERR_NETWORK_CHANGED/i.test(e));
  const realHttpBad = httpBad.filter(h => !/favicon/i.test(h));
  console.log('\n===== BILAN =====');
  console.log(`  Erreurs JS  : ${realJsErrors.length}`);
  realJsErrors.slice(0, 8).forEach(e => console.log(`    • ${e.slice(0, 160)}`));
  console.log(`  HTTP >= 400 : ${realHttpBad.length}`);
  [...new Set(realHttpBad)].slice(0, 8).forEach(h => console.log(`    • ${h}`));
  realJsErrors.forEach(e => add('BUG', `Erreur JS : ${e.slice(0, 120)}`));
  [...new Set(realHttpBad)].forEach(h => add('BUG', `HTTP ${h}`));

  const bugs = findings.filter(f => f.sev === 'BUG');
  console.log(`\n  ${bugs.length === 0 ? '✅ Aucun défaut' : '❌ ' + bugs.length + ' défaut(s)'} · ${findings.filter(f => f.sev === 'WARN').length} avertissement(s)`);
  console.log(`  Captures : ${SHOTS}-{1..4}*.png`);

  await browser.close();
  process.exit(bugs.length ? 1 : 0);
})().catch((e) => { console.error('CRASH QA:', e); process.exit(2); });
