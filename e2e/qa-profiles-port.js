/**
 * QA ciblée — page "Espaces Profils" (Gestion Volumétrie Profils) après portage Linux.
 *
 * Avant : le chemin dégradé Linux renvoyait un dict SANS status:success -> toast
 * "Erreur mise à jour: Inconnue" + tableau bloqué sur "Chargement des profils en cours...".
 * Après : la volumétrie R: + les profils remontent via l'agent broker.
 *
 * Lecture seule (GET /api/profiles-disk-info) — aucune action mutante.
 * Env : QA_BASE (def 127.0.0.1:5050), QA_USER (def administrateur), QA_PASS (requis).
 */
const { chromium } = require('./lib/playwright');
const { armProdSafe } = require('./lib/prod-safe');
const BASE = process.env.QA_BASE || 'http://127.0.0.1:5050';
const USER = process.env.QA_USER || 'administrateur';
const PASS = process.env.QA_PASS;
const CHROME = '/usr/bin/chromium';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  if (!PASS) { console.error('QA_PASS requis'); process.exit(2); }
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
  const page = await (await browser.newContext({ locale: 'fr-FR' })).newPage();
  await armProdSafe(page);   // 🛡️ aucune action mutante ne sort vers la ferme de prod
  page.setDefaultTimeout(8000);
  page.on('dialog', (d) => d.dismiss().catch(() => {}));

  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"]', USER);
  await page.fill('input[name="password"]', PASS);
  await Promise.all([page.waitForLoadState('domcontentloaded').catch(() => {}), page.click('button[type="submit"]')]);

  await page.goto(`${BASE}/profiles`, { waitUntil: 'domcontentloaded' });
  // Attendre que le tableau soit rempli (sortie de "Chargement des profils en cours...")
  let loaded = false;
  for (let i = 0; i < 15; i++) {
    const txt = await page.evaluate(() => document.getElementById('profiles-body')?.innerText || '');
    if (txt && !/Chargement des profils en cours/i.test(txt)) { loaded = true; break; }
    await sleep(700);
  }

  const out = await page.evaluate(() => {
    const txt = (id) => document.getElementById(id)?.innerText?.trim() || '';
    return {
      total: txt('stat-total-gb'),
      used: txt('stat-used-gb'),
      free: txt('stat-free-gb'),
      pct: txt('stat-percent-used'),
      title: txt('profiles-count-title'),
      location: txt('profiles-location'),
      rows: document.querySelectorAll('#profiles-body tr[data-profile]').length,
      bodyText: document.getElementById('profiles-body')?.innerText?.slice(0, 400) || '',
      toasts: (document.getElementById('toast-container')?.innerText || '').slice(0, 300),
      pageText: document.body.innerText,
    };
  });

  await browser.close();

  const noErrToast = !/Erreur mise à jour|Erreur chargement disque|Impossible de charger/i.test(out.toasts) &&
                     !/Erreur mise à jour|Erreur chargement disque/i.test(out.pageText);
  const diskOk = out.total !== '---' && /GB/.test(out.total) && out.pct !== '---';
  const tableOk = loaded && out.rows >= 1;

  const result = {
    loaded, diskOk, tableOk, noErrToast,
    stats: { total: out.total, used: out.used, free: out.free, pct: out.pct },
    title: out.title, location: out.location, rows: out.rows,
    bodySample: out.bodyText.replace(/\s+/g, ' ').slice(0, 200),
    toasts: out.toasts.replace(/\s+/g, ' '),
    jsErrors: errors.slice(0, 5),
  };
  console.log(JSON.stringify(result, null, 2));
  const ok = diskOk && tableOk && noErrToast;
  console.log(ok ? '\n✅ Page Profils OK : volumétrie R: + profils affichés, aucun toast d\'erreur.'
                 : '\n⚠️ Régression — au moins une assertion échoue.');
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
