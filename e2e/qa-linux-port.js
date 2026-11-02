/**
 * QA portage Linux — SysAdminMaster Dashboard.
 *
 * Pilote les 9 pages en Chromium headless : login formulaire, puis pour chaque page
 *  - capture les exceptions JS (pageerror), erreurs console, et réponses HTTP >= 400 ;
 *  - clique chaque bouton visible (borné, timeouts courts, dialogs auto-annulés).
 *
 * 🛡️ PROTECTION PROD : tous les POST mutants (actions agents, admin, profil, shell…)
 *    sont INTERCEPTÉS et mockés (page.route) — aucune commande réelle n'atteint la
 *    ferme RDS de production. Les GET (lecture seule) passent normalement.
 *
 * Env : QA_BASE (def http://127.0.0.1:5050), QA_USER (def administrateur), QA_PASS (requis).
 * Sortie : JSON sur stdout + /tmp/sam_qa_report.json
 */
const { chromium } = require('./lib/playwright');
const { armProdSafe } = require('./lib/prod-safe');

const BASE = process.env.QA_BASE || 'http://127.0.0.1:5050';
const USER = process.env.QA_USER || 'administrateur';
const PASS = process.env.QA_PASS;
const CHROME = '/usr/bin/chromium';

const PAGES = ['dashboard', 'sessions', 'profiles', 'resources', 'tools', 'reports', 'agents', 'logs', 'optimization'];

// Endpoints mutants : interceptés/mockés (jamais exécutés en prod)
const MUTATING = [
  '/api/actions', '/api/admin/agent-action', '/api/admin/master-action',
  '/api/profile-action', '/api/shell', '/api/tools',
  '/api/profiles-disk-info-refresh', '/api/master-config-reload',
];
const MOCK_BODY = JSON.stringify({
  status: 'success', success: true, message: '[QA mock] intercepté (prod protégée)',
  output: '[QA mock] aucune action réelle', commandId: 'qa-mock', command_id: 'qa-mock',
  stdout: '[QA mock]', master: { reachable: true },
});

// Bruit console à ignorer (non bloquant)
const IGNORE = [/favicon/i, /socket\.io/i, /Failed to load resource.*404.*favicon/i];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  if (!PASS) { console.error('QA_PASS requis'); process.exit(2); }
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
  const ctx = await browser.newContext({ locale: 'fr-FR', viewport: { width: 1366, height: 900 } });

  // Interception GLOBALE des POST mutants
  await ctx.route('**/*', (route) => {
    const req = route.request();
    const url = req.url();
    if (req.method() === 'POST' && MUTATING.some((m) => url.includes(m))) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: MOCK_BODY });
    }
    return route.continue();
  });

  const page = await ctx.newPage();
  await armProdSafe(page);   // 🛡️ enforcement réseau (le mock par page.route seul ne couvrait pas tout)
  page.setDefaultTimeout(5000);
  page.setDefaultNavigationTimeout(9000);
  page.on('dialog', (d) => d.dismiss().catch(() => {})); // annule confirm/alert/prompt

  // --- Login ---
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"]', USER);
  await page.fill('input[name="password"]', PASS);
  await Promise.all([page.waitForLoadState('domcontentloaded').catch(() => {}), page.click('button[type="submit"]')]);
  await sleep(1000);
  if (/\/login/.test(page.url())) { console.error('❌ login échoué'); await browser.close(); process.exit(2); }

  const report = { base: BASE, pages: {}, totals: { pageerrors: 0, consoleErrors: 0, http4xx5xx: 0, buttonsClicked: 0, buttonErrors: 0 } };

  for (const slug of PAGES) {
    const findings = { pageerrors: [], consoleErrors: [], httpErrors: [], buttons: { total: 0, clicked: 0, errors: [] } };

    const onPageError = (err) => findings.pageerrors.push(String(err).slice(0, 300));
    const onConsole = (msg) => {
      if (msg.type() !== 'error') return;
      const t = msg.text();
      if (IGNORE.some((re) => re.test(t))) return;
      findings.consoleErrors.push(t.slice(0, 300));
    };
    const onResponse = (resp) => {
      const s = resp.status();
      if (s < 400) return;
      const u = resp.url();
      if (IGNORE.some((re) => re.test(u))) return;
      findings.httpErrors.push({ status: s, url: u.replace(BASE, ''), method: resp.request().method() });
    };
    page.on('pageerror', onPageError);
    page.on('console', onConsole);
    page.on('response', onResponse);

    try {
      await page.goto(`${BASE}/${slug}`, { waitUntil: 'domcontentloaded' });
      await sleep(2500); // laisser les fetch initiaux se résoudre
    } catch (e) {
      findings.pageerrors.push('NAV: ' + String(e).slice(0, 200));
    }

    // Clic des boutons visibles (borné). Les confirm() sont annulés, les POST mutants mockés.
    try {
      const buttons = await page.$$('button:visible, [onclick]:visible, .btn:visible');
      findings.buttons.total = buttons.length;
      const MAX = 40;
      for (let i = 0; i < Math.min(buttons.length, MAX); i++) {
        try {
          const b = buttons[i];
          const label = ((await b.innerText().catch(() => '')) || (await b.getAttribute('title').catch(() => '')) || '').trim().slice(0, 40);
          await Promise.race([b.click({ timeout: 2500 }).catch(() => {}), sleep(2600)]);
          findings.buttons.clicked++;
          await sleep(180);
          // refermer un éventuel panneau/modal en pressant Echap
          await page.keyboard.press('Escape').catch(() => {});
        } catch (e) {
          findings.buttons.errors.push(String(e).slice(0, 120));
        }
      }
    } catch (e) {
      findings.buttons.errors.push('SCAN: ' + String(e).slice(0, 120));
    }

    await sleep(600);
    page.off('pageerror', onPageError);
    page.off('console', onConsole);
    page.off('response', onResponse);

    report.pages[slug] = findings;
    report.totals.pageerrors += findings.pageerrors.length;
    report.totals.consoleErrors += findings.consoleErrors.length;
    report.totals.http4xx5xx += findings.httpErrors.length;
    report.totals.buttonsClicked += findings.buttons.clicked;
    report.totals.buttonErrors += findings.buttons.errors.length;
  }

  await browser.close();
  const fs = require('fs');
  fs.writeFileSync('/tmp/sam_qa_report.json', JSON.stringify(report, null, 2));

  // Résumé lisible
  console.log('\n===== QA PORTAGE LINUX — RÉSUMÉ =====');
  for (const slug of PAGES) {
    const f = report.pages[slug];
    const flag = (f.pageerrors.length || f.consoleErrors.length || f.httpErrors.length) ? '⚠️ ' : '✅ ';
    console.log(`${flag}${slug.padEnd(13)} JS:${f.pageerrors.length} console:${f.consoleErrors.length} http≥400:${f.httpErrors.length} boutons:${f.buttons.clicked}/${f.buttons.total}`);
    f.pageerrors.forEach((e) => console.log(`     JS  ▸ ${e}`));
    f.consoleErrors.forEach((e) => console.log(`     CON ▸ ${e}`));
    f.httpErrors.forEach((e) => console.log(`     NET ▸ ${e.method} ${e.status} ${e.url}`));
  }
  console.log('\nTotaux:', JSON.stringify(report.totals));
  console.log('Rapport complet: /tmp/sam_qa_report.json');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
