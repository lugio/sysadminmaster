#!/usr/bin/env bash
# Harnais QA end-to-end de SysAdminMaster — lance les scripts et affiche un rapport compact.
#
# Usage : ./e2e/run-tests.sh [--gate|--all|--list|<script.js>…]
#
#   --gate  : liste EXPLICITE de scripts fiables et rapides, à passer AVANT tout
#             commit non trivial. Ne dépend d'AUCUN aller-retour vers un agent :
#             la gate doit rester verte et rapide même flotte éteinte, sinon elle
#             cesse d'être un garde-fou de commit pour devenir un test d'infra.
#   --all   : tous les scripts qa-*.js, y compris ceux qui exigent une flotte vivante.
#   --list  : affiche l'inventaire et sort.
#
# ⚠️ CES TESTS PILOTENT LA FERME RDS DE PRODUCTION.
#    Les scripts de la gate arment `e2e/lib/prod-safe.js` : une interception RÉSEAU
#    (page.route) qui applique une ALLOWLIST d'actions lecture seule. Tout le reste
#    est bloqué avant de partir et remplacé par une réponse 200 factice — l'UI se
#    comporte normalement, donc les défauts d'affichage restent détectables.
#    Ne jamais lever ce garde-fou, ne jamais élargir l'allowlist sans preuve que
#    l'action ne touche pas l'hôte.
#
#    Pourquoi au niveau réseau : la protection précédente était une regex sur
#    l'attribut `onclick` et le libellé des boutons. Elle est aveugle aux boutons
#    câblés en addEventListener. Le 2026-07-30, un run a réellement exécuté
#    clear_temp / flush_dns / optimize_ram / restart_spooler sur l'hôte broker, en prod.
#
#    ⚠️ Les 3 scripts HORS gate (FLEET_FILES) n'arment PAS ce garde-fou : leur objet
#    est justement le round-trip réel vers un agent. `qa-snapshot-shadow-errors.js`
#    déclenche shadow/screenshot sur des sessions RÉELLES d'utilisateurs — à ne
#    lancer qu'en connaissance de cause, hors heures ouvrées.
#
# ⚠️ Pourquoi des scripts node autonomes et pas `playwright test` :
#    chaque script appelle chromium.launch() au niveau racine. Passés au test-runner
#    Playwright, ils lanceraient un navigateur pendant la COLLECTE et feraient
#    échouer toute la suite (piège déjà rencontré sur un autre projet). On les exécute donc comme
#    des processus node, séquentiellement.

set -uo pipefail
cd "$(dirname "$0")/.."

# Scripts de la gate — ordre = priorité
GATE_FILES=(
  e2e/qa-audit-full.js      # 11 pages + clic de chaque bouton non destructif
  e2e/qa-profiles-port.js   # page Espaces Profils (volumétrie R:, liste profils)
  e2e/qa-alerts-guard.js    # PROUVE que prod-safe bloque l'envoi de mail réel
  e2e/qa-office-repair-modal.js  # les DEUX gestes Office nommés + le corps POST réellement envoyé
)

# Scripts hors gate : exigent une flotte vivante (aller-retour réel vers un agent)
FLEET_FILES=(
  e2e/qa-linux-port.js              # 1re passe portage Linux (rendu des pages)
  e2e/qa-profile-analyze.js         # round-trip réel : analyse de profil
  e2e/qa-snapshot-shadow-errors.js  # remontée d'erreur snapshot/shadow
  e2e/qa-profiles-public.js         # Espaces Profils via l'URL PUBLIQUE (reverse proxy)
)

MODE="${1:---gate}"

if [ "$MODE" = "--list" ]; then
  echo "Gate (sans dépendance agent) :"; printf '  %s\n' "${GATE_FILES[@]}"
  echo "Hors gate (flotte vivante requise) :"; printf '  %s\n' "${FLEET_FILES[@]}"
  exit 0
fi

case "$MODE" in
  --gate) TARGETS=("${GATE_FILES[@]}"); LABEL="GATE (${#GATE_FILES[@]} scripts, sans dépendance agent)" ;;
  --all)  TARGETS=("${GATE_FILES[@]}" "${FLEET_FILES[@]}"); LABEL="COMPLET (${#GATE_FILES[@]}+${#FLEET_FILES[@]} scripts)" ;;
  *)      TARGETS=("$@"); LABEL="ciblé (${#TARGETS[@]} script(s))" ;;
esac

# Identifiants : SAM_ADMIN_PASSWORD vit dans .env — jamais affiché
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export QA_BASE="${QA_BASE:-http://127.0.0.1:5050}"
export QA_USER="${QA_USER:-administrateur}"
export QA_PASS="${QA_PASS:-${SAM_ADMIN_PASSWORD:-}}"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SysAdminMaster E2E — $(date '+%Y-%m-%d %H:%M:%S')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Mode   : $LABEL"
echo "  Cible  : $QA_BASE (utilisateur : $QA_USER)"

if [ -z "$QA_PASS" ]; then
  echo ""
  echo "  ✗ QA_PASS absent — introuvable dans .env (SAM_ADMIN_PASSWORD) et non fourni."
  echo "    Les scripts exigent une session authentifiée."
  exit 2
fi

# Le dashboard doit répondre, sinon tous les scripts échoueront de la même façon
if ! curl -sf -o /dev/null --max-time 5 "$QA_BASE/login"; then
  echo ""
  echo "  ✗ Dashboard injoignable sur $QA_BASE"
  echo "    → systemctl status sam-dashboard   /   journalctl -u sam-dashboard -n 30"
  exit 2
fi

echo ""

FAILED=()
PASSED=()
for script in "${TARGETS[@]}"; do
  if [ ! -f "$script" ]; then
    echo "  ✗ $script — INTROUVABLE"; FAILED+=("$script (introuvable)"); continue
  fi
  printf '  ▶ %s\n' "$script"
  START=$(date +%s)
  if node "$script"; then
    echo "  ✓ $script  ($(($(date +%s) - START))s)"; PASSED+=("$script")
  else
    CODE=$?
    echo "  ✗ $script  (sortie $CODE, $(($(date +%s) - START))s)"; FAILED+=("$script (sortie $CODE)")
  fi
  echo ""
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Réussis : ${#PASSED[@]} / $((${#PASSED[@]} + ${#FAILED[@]}))"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "  Échecs  :"; printf '    - %s\n' "${FAILED[@]}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  exit 1
fi

if [ "$MODE" = "--gate" ]; then
  echo ""
  echo "  ⚠️  Gate verte = « le dashboard ne casse pas ». Ce n'est PAS la preuve que"
  echo "     la ferme RDS fonctionne : aucun aller-retour agent n'est testé ici."
  echo "     Pour ça : ./e2e/run-tests.sh --all (exige une flotte vivante)."
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
exit 0
