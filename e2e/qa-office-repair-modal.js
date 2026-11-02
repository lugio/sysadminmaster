/**
 * QA — modale de reparation Office (v2.28.0, etendue 2.29.4 : jeton OK).
 *
 * Verifie ce que l'utilisateur VOIT et ENVOIE, pas l'etat interne :
 *  - la modale propose DEUX gestes nommes (licence / identite) ;
 *  - le geste par defaut suit le statut affiche ("licence" si perime,
 *    "identite" si OK) et la modale se reinitialise entre deux ouvertures ;
 *  - un jeton OK a AUSSI son bouton (2.29.4) — le cache d'identite casse
 *    avec un jeton valide, et ce bouton etait absent exactement dans ce cas ;
 *  - la precondition affichee est la BONNE (session ouverte + Office ferme) —
 *    l'ancienne note disait l'inverse, et le disait depuis toujours ;
 *  - le corps POST porte bien `kind`, et pas l'ancien `include_identity`.
 *
 * Env : QA_BASE (def 127.0.0.1:5050), QA_USER (def administrateur), QA_PASS (requis).
 */
const { chromium } = require('./lib/playwright');
const { armProdSafe } = require('./lib/prod-safe');
const BASE = process.env.QA_BASE || 'http://127.0.0.1:5050';
const USER = process.env.QA_USER || 'administrateur';
const PASS = process.env.QA_PASS;
const CHROME = '/usr/bin/chromium';

(async () => {
  if (!PASS) { console.error('QA_PASS requis'); process.exit(2); }
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
  const page = await (await browser.newContext({ locale: 'fr-FR' })).newPage();
  await armProdSafe(page);
  page.setDefaultTimeout(8000);
  page.on('dialog', (d) => d.dismiss().catch(() => {}));

  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"]', USER);
  await page.fill('input[name="password"]', PASS);
  await Promise.all([page.waitForLoadState('domcontentloaded').catch(() => {}), page.click('button[type="submit"]')]);
  await page.goto(`${BASE}/licenses`, { waitUntil: 'domcontentloaded' });

  const fails = [];
  const ok = (cond, label) => { console.log(`  ${cond ? '✅' : '❌'} ${label}`); if (!cond) fails.push(label); };

  // 1. La note de la page dit la bonne precondition.
  const note = await page.evaluate(() => document.body.innerText);
  ok(/connecté/i.test(note) && /Office fermé/i.test(note), 'la note annonce « connecté » + « Office fermé »');
  ok(!/doit être déconnecté/i.test(note), "l'ancienne consigne « doit être déconnecté » a disparu");

  // 2. La modale propose deux gestes nommes.
  // ⚠️ On passe par le CHEMIN DE L'UTILISATEUR : on stube la LECTURE
  // /api/licenses pour qu'un jeton perime existe, puis on clique le VRAI
  // bouton. Injecter `window._hosts` ne marche pas — `_hosts` est declaree en
  // `let`, donc absente de window (meme piege que `_dlSimulated`, piege 25) :
  // la 1re version de ce test echouait sur du vide, et aurait pu faire croire
  // a un defaut de la page.
  // ⚠️ Le motif DOIT accepter la query string : la page appelle desormais
  // /api/licenses?scope=o365. Un glob sans `*` ne matche pas, le stub ne
  // s'applique pas, et le test se retrouve a travailler sur les VRAIES
  // donnees de la ferme — donc a ne rien prouver de reproductible.
  await page.route('**/api/licenses*', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ status: 'ok', totals: { ok: 0, stale: 1, missing: 0 }, hosts: [{
      hostname: 'SRVQA', sca_status: 'ok', office_version: '16.0.0.0', online: true,
      users_ok: 0, users_stale: 1, users_missing: 0,
      session_users: ['qa.user'],
      users: [
        { name: 'qa.user', status: 'stale', age_days: 42, has_session: true, scope: 'o365' },
        { name: 'qa.absent', status: 'stale', age_days: 51, has_session: false, scope: 'o365' },
        // Jeton OK + session : le cas du 08/09 (un utilisateur, 48v35 avec un
        // jeton de 1,2 j). Avant 2.29.4 cette ligne n'avait AUCUN bouton.
        { name: 'qa.ok', status: 'ok', age_days: 1.2, has_session: true, scope: 'o365' },
      ] }] }),
  }));
  await page.reload({ waitUntil: 'domcontentloaded' });
  // Le bouton Reparer vit dans le detail de l'hote : on suit le meme parcours
  // qu'un administrateur (ligne d'hote -> Detail -> Reparer).
  await page.waitForSelector('button[onclick^="showDetail"]', { state: 'visible' });
  await page.click('button[onclick^="showDetail"]');
  await page.waitForSelector('button[onclick^="openRepairModal"]', { state: 'visible' });

  // D — un bouton qui ne peut pas aboutir ne doit pas etre cliquable (piege 24).
  // qa.absent n'a pas de session : son profil UPD n'est pas monte, la reparation
  // echouerait sur "Profil introuvable" 90 s plus tard.
  const dis = await page.$$eval('button[disabled][title]', els => els.map(e => e.getAttribute('title')));
  ok(dis.length >= 1, `le bouton d'un utilisateur sans session est désactivé (${dis.length})`);
  ok(dis.some(t => /session fermée/i.test(t || '')), "le bouton désactivé dit POURQUOI (session fermée)");
  const actifs = await page.$$eval('button[onclick^="openRepairModal"]', els => els.length);
  ok(actifs === 2, `les deux utilisateurs avec session ont un bouton actif, jeton périmé OU OK (${actifs})`);

  // E — jeton OK : le bouton existe et la modale s'ouvre sur « identité »
  // (renouveler un jeton valide ne répare rien — piège 34, cas du 08/09).
  await page.click('button[onclick="openRepairModal(0, 2)"]');
  await page.waitForSelector('#repair-modal', { state: 'visible' });
  const okUser = await page.$eval('#repair-user', e => e.textContent);
  ok(okUser === 'qa.ok', `la modale vise bien l'utilisateur à jeton OK (${okUser})`);
  const okDefault = await page.$eval('input[name="repair-kind"][value="identity"]', e => e.checked);
  ok(okDefault, 'sur un jeton OK, le geste par défaut est « identité »');
  await page.click('#repair-cancel-btn');

  await page.click('button[onclick="openRepairModal(0, 0)"]');
  await page.waitForSelector('#repair-modal', { state: 'visible' });
  const kinds = await page.$$eval('input[name="repair-kind"]', els => els.map(e => ({ v: e.value, c: e.checked })));
  ok(kinds.length === 3, `trois gestes proposés (trouvé ${kinds.length})`);
  ok(kinds.some(k => k.v === 'license' && k.c), 'le geste « licence » est coché par défaut');
  ok(kinds.some(k => k.v === 'broker'), 'le geste « broker Azure AD » est proposé (3e cause, 23/09)');
  const modalTxt = await page.evaluate(() => document.getElementById('repair-modal').innerText);
  ok(/licence/i.test(modalTxt) && /identité/i.test(modalTxt), 'les deux gestes sont nommés en toutes lettres');
  ok(/0x80070002/.test(modalTxt), "le symptôme du cache d'identité est cité (0x80070002)");
  ok(/AADSTS50196/.test(modalTxt), 'le symptôme de la boucle broker est cité (AADSTS50196)');

  // 2 bis. Le DIAGNOSTIC est joignable. Sans ce test, l'action pourrait être
  // whitelistée côté serveur et restée injoignable depuis l'UI (piège 14),
  // exactement comme le bouton « Réparer » absent sur un jeton OK (piège 34).
  const diagVisible = await page.$eval('#repair-diag-btn', e => !e.disabled && e.offsetParent !== null);
  ok(diagVisible, 'le bouton Diagnostiquer est présent et actif');

  // 3. Le choix ne persiste pas d'une ouverture a l'autre.
  await page.check('input[name="repair-kind"][value="identity"]');
  await page.click('#repair-cancel-btn');
  await page.click('button[onclick="openRepairModal(0, 0)"]');
  await page.waitForSelector('#repair-modal', { state: 'visible' });
  const reset = await page.$eval('input[name="repair-kind"][value="license"]', e => e.checked);
  ok(reset, 'la modale se réinitialise sur « licence » à la réouverture');

  // 4. Le corps POST porte `kind` — lu sur la requete reelle, pas sur le code.
  await page.check('input[name="repair-kind"][value="identity"]');
  let body = null;
  page.on('request', r => { if (r.url().includes('/api/licenses/repair') && r.method() === 'POST') { try { body = JSON.parse(r.postData() || '{}'); } catch (e) {} } });
  await page.click('#repair-confirm-btn');
  await page.waitForTimeout(1500);
  ok(body !== null, 'la requête de réparation est bien partie');
  ok(body && body.kind === 'identity', `le corps porte kind=identity (reçu : ${JSON.stringify(body)})`);
  ok(body && body.include_identity === undefined, "l'ancien champ include_identity n'est plus envoyé");

  // 5. Le diagnostic part sur SA route, avec hostname + user. Lu sur la requete
  //    reelle : c'est une divergence de CORPS (et non d'URL) qui a produit les
  //    trois defauts muets du 27/08 - check-endpoints.py ne voit pas le corps.
  await page.click('#repair-cancel-btn');
  await page.click('button[onclick="openRepairModal(0, 0)"]');
  await page.waitForSelector('#repair-modal', { state: 'visible' });
  let diagBody = null;
  page.on('request', r => {
    if (r.url().includes('/api/licenses/diagnose') && r.method() === 'POST') {
      try { diagBody = JSON.parse(r.postData() || '{}'); } catch (e) {}
    }
  });
  await page.click('#repair-diag-btn');
  await page.waitForTimeout(1500);
  ok(diagBody !== null, 'la requête de diagnostic est bien partie');
  ok(diagBody && diagBody.user && diagBody.hostname,
     `le corps du diagnostic porte hostname + user (reçu : ${JSON.stringify(diagBody)})`);

  ok(errors.length === 0, `aucune erreur JS (${errors.length})`);
  if (errors.length) errors.slice(0, 5).forEach(e => console.log('     ' + e));

  await browser.close();
  console.log(`\n${fails.length === 0 ? '✅' : '❌'} modale de réparation Office : ${fails.length} défaut(s)`);
  process.exit(fails.length === 0 ? 0 : 1);
})();
