/**
 * AUDIT COMPLET — toutes les pages du Dashboard + balayage des boutons NON destructifs.
 *
 * Objectif : ne plus découvrir les pages cassées une par une.
 * Pour CHAQUE page : on charge, on laisse retomber (4s), et on capture
 *   - les erreurs JS console / pageerror
 *   - les réponses réseau >= 400 (GET/POST déclenchés par la page)
 *   - les toasts d'erreur apparus au chargement (Erreur / Inconnue / Timeout / Impossible / échec)
 * Puis on CLIQUE chaque bouton NON destructif (denylist) et on recapture les erreurs/toasts.
 *
 * Sécurité prod : les actions mutantes (logoff, disconnect, restart, clean, delete,
 * message, optimiser, update, deploy...) sont EXCLUES par denylist sur l'attribut onclick.
 * Les dialogs (confirm/prompt/alert) sont DISMISS -> même si un bouton mutant passait
 * la denylist, son confirm() serait annulé.
 *
 * Env : QA_BASE (def 127.0.0.1:5050), QA_USER, QA_PASS (requis).
 */
const { chromium } = require('./lib/playwright');
const { armProdSafe } = require('./lib/prod-safe');
const BASE = process.env.QA_BASE || 'http://127.0.0.1:5050';
const USER = process.env.QA_USER || 'administrateur';
const PASS = process.env.QA_PASS;
const CHROME = '/usr/bin/chromium';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PAGES = ['/dashboard', '/sessions', '/profiles', '/resources', '/tools', '/reports', '/agents', '/logs', '/optimization', '/licenses', '/alerts'];
// Denylist de LIBELLÉS — 2ᵉ ligne de défense uniquement. L'autorité est armProdSafe()
// (interception réseau) : cette regex ne voit ni les boutons câblés en addEventListener
// ni les libellés-icônes. Élargie le 30/07 aux motifs qui avaient laissé passer
// clear_temp / flush_dns / repair (incident du jour).
const DENY = /logoff|disconnect|deconnect|restart|redemarr|reboot|delete|supprim|\bclean|nettoy|cleanup|purge|vider|flush|kill|\bstop\b|shutdown|message|envoyer|\bsend\b|deploy|deploiement|update|mise.?a.?jour|optimiz|optimis|remove|force|reset|terminer|repair|repar|bootstrap|maintenance|install|desinstall|creer|create|ajouter/i;
const ERR_TOAST = /Erreur|Inconnue|Timeout|Impossible|échec|echec|introuvable|non autoris/i;
// Bruit connu non bloquant (faux 500 connecteurs/3CX déjà constatés hors périmètre portage)
const NOISE_URL = /\/api\/group\/objectifs|\/api\/3cx|favicon/i;

(async () => {
  if (!PASS) { console.error('QA_PASS requis'); process.exit(2); }
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
  const ctx = await browser.newContext({ locale: 'fr-FR' });
  const page = await ctx.newPage();
  page.setDefaultTimeout(6000);
  page.on('dialog', (d) => d.dismiss().catch(() => {}));

  // 🛡️ Garde-fou prod-safe AU NIVEAU RÉSEAU — la denylist sur les libellés de
  // boutons (DENY ci-dessus) n'est qu'un premier filtre : elle est aveugle aux
  // boutons câblés en addEventListener et aux libellés-icônes. Le 2026-07-30, un
  // run a réellement exécuté clear_temp / flush_dns / optimize_ram / restart_spooler
  // sur l'hôte broker en production. Seule cette interception fait autorité.
  const guard = await armProdSafe(page);

  let current = '(login)';
  const consoleErrors = [];
  const failedResp = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push({ page: current, text: m.text().slice(0, 200) }); });
  page.on('pageerror', (e) => consoleErrors.push({ page: current, text: 'pageerror: ' + e.message.slice(0, 200) }));
  page.on('response', (r) => {
    try { const s = r.status(); const u = r.url(); if (s >= 400 && !NOISE_URL.test(u)) failedResp.push({ page: current, status: s, url: u.replace(BASE, '') }); } catch (e) {}
  });

  // Login
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"]', USER);
  await page.fill('input[name="password"]', PASS);
  await Promise.all([page.waitForLoadState('domcontentloaded').catch(() => {}), page.click('button[type="submit"]')]);
  await sleep(1500);

  const report = [];
  let problems = 0;
  console.log('\n================ AUDIT COMPLET DASHBOARD ================\n');
  const clearToasts = () => page.evaluate(() => { const c = document.getElementById('toast-container'); if (c) c.innerHTML = ''; }).catch(() => {});
  const killOverlays = () => page.evaluate(() => {
    // Les modales statiques du template (présentes en permanence) : on les MASQUE,
    // on ne les retire pas — sinon un poller en vol (analyze) écrirait dans un noeud
    // disparu et lèverait un faux positif d'audit.
    //
    // ⚠️ Cette liste était codée en dur, donc à maintenir à la main : toute page
    // ajoutant une modale devait y penser, sans quoi l'audit la supprimait puis
    // signalait le `null` qui s'ensuit comme un défaut de la PAGE. Piégé le
    // 28/08/2026 par la modale de /alerts. Un faux positif coûte plus cher que
    // pas d'audit : il envoie chercher un bug qui n'existe pas.
    // ➡️ Le marqueur `data-static-modal` rend le contrat auto-portant — une
    //    nouvelle modale de template se déclare elle-même, sans toucher au test.
    const STATIC = ['live-view-modal', 'analysis-modal'];
    const isStatic = (e) => STATIC.includes(e.id) || e.hasAttribute('data-static-modal');
    STATIC.forEach((id) => { const m = document.getElementById(id); if (m) m.style.display = 'none'; });
    document.querySelectorAll('[data-static-modal]').forEach((m) => { m.style.display = 'none'; });
    document.querySelectorAll('div[style*="position:fixed"][style*="z-index:9"]').forEach((e) => {
      if (isStatic(e)) return;
      try { e.remove(); } catch (x) {}
    });
  }).catch(() => {});

  for (const path of PAGES) {
    current = path;
    const before = { ce: consoleErrors.length, fr: failedResp.length };
    await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded' }).catch(() => {});
    await sleep(4000); // laisser les fetch de la page retomber

    const loadToasts = await page.evaluate(() => document.getElementById('toast-container')?.innerText || '').catch(() => '');
    const loadToastErr = ERR_TOAST.test(loadToasts) ? loadToasts.replace(/\s+/g, ' ').slice(0, 200) : '';

    // Enumérer les boutons NON destructifs
    const buttons = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('button, [onclick]').forEach((el, i) => {
        const oc = el.getAttribute('onclick') || '';
        const label = (el.innerText || el.getAttribute('title') || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 40);
        out.push({ i, oc: oc.slice(0, 80), label });
      });
      return out;
    }).catch(() => []);
    const safe = buttons.filter((b) => (b.oc || b.label) && !DENY.test(b.oc + ' ' + b.label));

    // Cliquer chaque bouton sûr, recapturer les toasts d'erreur
    const btnErrors = [];
    for (const b of safe.slice(0, 14)) {
      await clearToasts();
      const ceBefore = consoleErrors.length, frBefore = failedResp.length;
      try {
        const handles = await page.$$('button, [onclick]');
        if (handles[b.i]) {
          await Promise.race([handles[b.i].click({ timeout: 2000 }).catch(() => {}), sleep(2200)]);
        }
      } catch (e) {}
      await sleep(800);
      const t = await page.evaluate(() => document.getElementById('toast-container')?.innerText || '').catch(() => '');
      if (ERR_TOAST.test(t)) btnErrors.push({ btn: b.label || b.oc, toast: t.replace(/\s+/g, ' ').slice(0, 140) });
      else if (consoleErrors.length > ceBefore) btnErrors.push({ btn: b.label || b.oc, jsErr: consoleErrors[consoleErrors.length - 1].text });
      await killOverlays();
    }

    const r = {
      page: path,
      loadToastErr,
      consoleErrors: consoleErrors.slice(before.ce).map((e) => e.text),
      failedResponses: failedResp.slice(before.fr).map((e) => `${e.status} ${e.url}`),
      safeButtons: safe.length,
      btnErrors,
    };
    report.push(r);
    // Impression INCREMENTALE (pas de perte si le run est coupé)
    const issues = [];
    if (r.loadToastErr) issues.push(`toast chargement: ${r.loadToastErr}`);
    r.failedResponses.forEach((x) => issues.push(`HTTP ${x}`));
    r.consoleErrors.forEach((x) => issues.push(`JS: ${x}`));
    r.btnErrors.forEach((x) => issues.push(`bouton [${x.btn}] -> ${x.toast || x.jsErr}`));
    problems += issues.length;
    console.log(`${issues.length ? '❌' : '✅'} ${r.page}  (${r.safeButtons} boutons sûrs testés, ${Math.min(r.safeButtons, 14)} cliqués)`);
    issues.forEach((x) => console.log('     • ' + x));
  }

  await browser.close();
  console.log(`\n=========== ${problems} problème(s) sur ${report.length} pages ===========`);
  guard.summary().forEach((l) => console.log(l));
  process.exit(problems ? 1 : 0);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
