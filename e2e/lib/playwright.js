/**
 * Résolution de Playwright pour les scripts QA de SysAdminMaster.
 *
 * Pourquoi ce module : les 5 scripts e2e chargeaient Playwright via un chemin
 * ABSOLU vers les node_modules d'un AUTRE projet
 * (`<other-project>/node_modules/playwright`). Une dépendance invisible,
 * non déclarée, qui casse dès que l'autre projet bouge ou disparaît.
 *
 * Ordre de résolution :
 *   1. node_modules local        → `npm install` dans ce repo (voie propre, déclarée
 *                                  dans package.json)
 *   2. QA_PLAYWRIGHT_PATH        → repli : chemin d'une installation voisine, pour
 *                                  ne pas imposer un `npm install` de Playwright +
 *                                  navigateurs (~400 Mo) quand une copie existe déjà
 *
 * Si rien n'est trouvé, on échoue avec un message actionnable plutôt qu'une
 * MODULE_NOT_FOUND opaque.
 */

const CANDIDATES = [
  'playwright',                                          // node_modules local
  ...(process.env.QA_PLAYWRIGHT_PATH ? [process.env.QA_PLAYWRIGHT_PATH] : []),  // repli
];

function loadPlaywright() {
  const tried = [];
  for (const candidate of CANDIDATES) {
    try {
      return { pw: require(candidate), from: candidate };
    } catch (err) {
      if (err.code !== 'MODULE_NOT_FOUND') throw err;   // vraie erreur → ne pas masquer
      tried.push(candidate);
    }
  }
  throw new Error(
    'Playwright introuvable. Emplacements essayés :\n  - ' + tried.join('\n  - ') +
    '\n\nInstaller la dépendance déclarée :\n' +
    '  cd <install-dir> && npm install\n' +
    '(ou QA_PLAYWRIGHT_PATH=<chemin>/node_modules/playwright)\n'
  );
}

const { pw, from } = loadPlaywright();

module.exports = {
  chromium: pw.chromium,
  firefox: pw.firefox,
  webkit: pw.webkit,
  playwrightPath: from,
};
