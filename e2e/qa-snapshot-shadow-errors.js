/**
 * QA ciblée — remontée d'erreur snapshot/shadow (portage Linux, fix front 2026-06-22).
 *
 * Drive le VRAI UI (aucun mock) : déclenche openLiveView (snapshot) et runAction('shadow')
 * sur des sessions réelles, puis vérifie que l'erreur de l'agent est AFFICHÉE
 * (au lieu du spinner infini / "Timeout shadow" générique).
 *
 * Actions screenshot/shadow = OBSERVATION (non destructif). Aucune commande mutante.
 * Env : QA_BASE, QA_USER (def administrateur), QA_PASS (requis).
 */
const { chromium } = require('./lib/playwright');
// Hosts of the scenario: a relay agent that predates screenshot/shadow, and an
// older agent (v2.8.2) whose capture fails with the Session 0 isolation error.
const RELAY_HOST = process.env.QA_RELAY_HOST || 'RDS-BROKER';
const OLD_AGENT_HOST = process.env.QA_OLD_AGENT_HOST || 'RDSH02';
const SESSION_USER = process.env.QA_SESSION_USER || 'jdoe';
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
  page.on('dialog', (d) => d.dismiss().catch(() => {}));

  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"]', USER);
  await page.fill('input[name="password"]', PASS);
  await Promise.all([page.waitForLoadState('domcontentloaded').catch(() => {}), page.click('button[type="submit"]')]);
  await page.goto(`${BASE}/sessions`, { waitUntil: 'domcontentloaded' });
  await sleep(2500);

  const out = {};
  // Attend qu'un marqueur texte apparaisse (round-trip agent variable selon relais/heartbeat).
  const waitForText = async (re, budgetMs) => {
    const t0 = Date.now();
    while (Date.now() - t0 < budgetMs) {
      const hit = await page.evaluate(() => document.body.innerText);
      if (re.test(hit)) return Math.round((Date.now() - t0) / 1000);
      await sleep(700);
    }
    return -1;
  };

  // 1) SNAPSHOT sur agent relais (RELAY_HOST) -> agent ancien "Action inconnue: screenshot"
  await page.evaluate(([u, h]) => window.openLiveView('3', u, h), [SESSION_USER, RELAY_HOST]);
  out.snapshot_relay_s = await waitForText(/Capture impossible/i, 25000);
  out.snapshot_relay = await page.evaluate(() => {
    const l = document.getElementById('live-loader');
    return l ? l.innerText.trim().slice(0, 160) : '(pas de loader)';
  });
  await page.evaluate(() => window.closeLiveView && window.closeLiveView());
  await sleep(500);

  // 2) SHADOW sur agent relais (RELAY_HOST) -> toast "Shadow impossible : Action inconnue: shadow"
  // runAction('shadow') passe par un confirm() : en usage réel l'utilisateur clique OK.
  // Le handler dialog du test dismiss() tout -> on force confirm=true pour reproduire le clic OK.
  await page.evaluate(() => { window.confirm = () => true; });
  await page.evaluate(([u, h]) => window.runAction('shadow', '3', u, h), [SESSION_USER, RELAY_HOST]);
  out.shadow_relay_s = await waitForText(/Shadow impossible/i, 25000);
  out.shadow_relay_toast = out.shadow_relay_s >= 0;
  await sleep(500);

  // 3) SNAPSHOT sur agent v2.8.2 (OLD_AGENT_HOST) -> erreur "Isolation Session 0"
  await page.evaluate(([u, h]) => window.openLiveView('10', u, h), [SESSION_USER, OLD_AGENT_HOST]);
  out.snapshot_v282_s = await waitForText(/Capture impossible/i, 20000);
  out.snapshot_v282 = await page.evaluate(() => {
    const l = document.getElementById('live-loader');
    return l ? l.innerText.trim().slice(0, 160) : '(pas de loader)';
  });

  await browser.close();
  console.log(JSON.stringify(out, null, 2));
  const ok = /impossible/i.test(out.snapshot_relay) && out.shadow_relay_toast === true && /impossible/i.test(out.snapshot_v282);
  console.log(ok ? '\n✅ Les erreurs agent sont désormais AFFICHÉES (plus de spinner infini / timeout générique).'
                 : '\n⚠️ Au moins un cas ne remonte pas l\'erreur — à revoir.');
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
