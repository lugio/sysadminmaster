/**
 * QA ciblée — bouton "Analyser" d'un profil (page Espaces Profils) après portage Linux.
 *
 * Avant : /api/profile-action était un STUB (clé `profile` vs `name` envoyé par le front,
 * command_id bidon) -> toast "Erreur analyse: Inconnue".
 * Après : la commande analyze_profile est routée vers l'agent broker ; le front
 * poll /api/command-status/<id> (clé `result`) et affiche la répartition par dossier.
 *
 * analyze_profile = LECTURE SEULE (mesure des tailles de dossiers). Aucune action mutante.
 * Env : QA_BASE (def 127.0.0.1:5050), QA_USER, QA_PASS (requis).
 */
const { chromium } = require('./lib/playwright');
const BASE = process.env.QA_BASE || 'http://127.0.0.1:5050';
const USER = process.env.QA_USER || 'administrateur';
const PASS = process.env.QA_PASS;
const CHROME = '/usr/bin/chromium';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  if (!PASS) { console.error('QA_PASS requis'); process.exit(2); }
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
  const page = await (await browser.newContext({ locale: 'fr-FR' })).newPage();
  page.setDefaultTimeout(8000);
  page.on('dialog', (d) => d.accept().catch(() => {}));

  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"]', USER);
  await page.fill('input[name="password"]', PASS);
  await Promise.all([page.waitForLoadState('domcontentloaded').catch(() => {}), page.click('button[type="submit"]')]);
  await page.goto(`${BASE}/profiles`, { waitUntil: 'domcontentloaded' });

  // Attendre le tableau rempli
  let firstProfile = null;
  for (let i = 0; i < 15; i++) {
    firstProfile = await page.evaluate(() => {
      const tr = document.querySelector('#profiles-body tr[data-profile]');
      return tr ? tr.getAttribute('data-profile') : null;
    });
    if (firstProfile) break;
    await sleep(700);
  }
  if (!firstProfile) { console.log('⚠️ aucun profil listé'); await browser.close(); process.exit(1); }

  // Déclencher l'analyse (round-trip agent ~15s)
  await page.evaluate((n) => window.analyzeProfile(n), firstProfile);

  let modalShown = false, modalText = '', errToast = '';
  const t0 = Date.now();
  while (Date.now() - t0 < 35000) {
    const st = await page.evaluate(() => {
      const m = document.getElementById('analysis-modal');
      const shown = m && getComputedStyle(m).display !== 'none';
      const toasts = document.getElementById('toast-container')?.innerText || '';
      return { shown, content: document.getElementById('modal-content')?.innerText?.slice(0, 300) || '', toasts };
    });
    if (st.shown) { modalShown = true; modalText = st.content; break; }
    if (/Erreur analyse|Erreur parsing|Erreur:|Inconnue|Délai dépassé/i.test(st.toasts)) { errToast = st.toasts.replace(/\s+/g, ' ').slice(0, 200); break; }
    await sleep(800);
  }

  await browser.close();
  const out = {
    profile: firstProfile,
    elapsed_s: Math.round((Date.now() - t0) / 1000),
    modalShown,
    modalSample: modalText.replace(/\s+/g, ' ').slice(0, 180),
    errToast,
  };
  console.log(JSON.stringify(out, null, 2));
  const ok = modalShown && /MB|GB|%/.test(modalText) && !errToast;
  console.log(ok ? '\n✅ Analyser OK : la répartition par dossier s\'affiche (round-trip agent broker réel).'
                 : '\n⚠️ Analyser KO — voir errToast.');
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
