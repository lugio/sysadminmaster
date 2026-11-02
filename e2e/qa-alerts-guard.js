/**
 * QA ciblée — le garde-fou prod-safe doit intercepter l'ENVOI DE MAIL RÉEL.
 *
 * Pourquoi ce script existe : `/api/alerts/test` expédie un vrai message aux
 * destinataires configurés. Il est listé dans `ALWAYS_BLOCK`, mais l'audit
 * général ne le PROUVE pas — le bouton passe par un `confirm()` que la QA
 * rejette, donc la requête ne part jamais et le garde-fou n'est jamais
 * sollicité. Une protection non exercée est une protection supposée.
 *
 * On appelle donc l'endpoint directement depuis la page, garde-fou armé, et on
 * vérifie DEUX choses : la réponse est bien la réponse factice, et l'historique
 * des contrôles n'a pas gagné de ligne (donc aucun mail n'est parti).
 *
 * Env : QA_PASS (requis), QA_USER (def administrateur).
 */
const { chromium } = require('./lib/playwright');
const { armProdSafe } = require('./lib/prod-safe');

const BASE = process.env.QA_BASE || 'http://127.0.0.1:5050';
const USER = process.env.QA_USER || 'administrateur';
const PASS = process.env.QA_PASS;
const CHROME = '/usr/bin/chromium';

if (!PASS) { console.error('QA_PASS requis'); process.exit(1); }

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const page = await browser.newPage();
  const tracker = await armProdSafe(page, { verbose: false });
  let failures = 0;

  const check = (ok, label, detail = '') => {
    console.log(`  ${ok ? '✅' : '❌'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!ok) failures++;
  };

  try {
    await page.goto(`${BASE}/login`);
    await page.fill('input[name=username]', USER).catch(() => {});
    await page.fill('input[name=password]', PASS);
    await Promise.all([page.waitForLoadState('domcontentloaded').catch(() => {}),
                       page.click('button[type="submit"]')]);
    await page.goto(`${BASE}/alerts`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);

    const before = await page.evaluate(async () =>
      (await (await fetch('/api/alerts/history?limit=200')).json()).runs.length);

    const res = await page.evaluate(async () => {
      const r = await fetch('/api/alerts/test', { method: 'POST' });
      return r.json();
    });

    const after = await page.evaluate(async () =>
      (await (await fetch('/api/alerts/history?limit=200')).json()).runs.length);

    console.log(`\n  Réponse reçue : ${JSON.stringify(res)}`);
    check(/bloquée avant la ferme/.test(res.message || ''),
          'la requête a été interceptée par le garde-fou',
          res.message ? '' : 'AUCUN marqueur prod-safe dans la réponse');
    check(after === before,
          "aucun contrôle enregistré, donc aucun mail expédié",
          `historique ${before} -> ${after}`);
    check(res.status === 'success',
          'la réponse factice respecte le contrat de la route (pas de branche erreur côté UI)');

    const blocked = (tracker.blocked || []).some((w) => String(w).includes('/api/alerts/test'));
    check(blocked, 'le blocage est bien tracé par le garde-fou');
  } catch (e) {
    console.error('  ❌ Exception :', e.message);
    failures++;
  } finally {
    await browser.close();
  }

  console.log(`\n${failures ? '❌' : '✅'} garde-fou envoi de mail : ${failures} défaut(s)`);
  process.exit(failures ? 1 : 0);
})();
