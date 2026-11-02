#!/usr/bin/env python3
"""
Garde-fou de version — vérifie que TOUTES les déclarations de version du dépôt
disent la même chose que le fichier `VERSION`.

Pourquoi ce script existe
-------------------------
Le projet a vécu avec 11 déclarations désynchronisées : `Web/routes/api.py`
annonçait 2.7.6 pendant que l'agent déployé sur la ferme tournait en 2.9.5. Un
diagnostic de panne mené sur une version fausse fait perdre des heures.

Le job CI ne comparait que deux fichiers (`VERSION` et `$script:AgentVersion`).
La dérive est immédiatement revenue : le bump 2.10.0 du 30/07/2026 a laissé
9 fichiers en 2.9.5, et la CI est restée verte. D'où ce script, qui liste
NOMMÉMENT chaque endroit qui déclare une version.

Ajouter une déclaration de version quelque part sans l'ajouter ici, c'est
rouvrir le trou. La liste SITES est donc la vraie source de vérité du contrat.

Usage
-----
    python3 tools/check-version.py          # code 0 si tout est aligné
    python3 tools/check-version.py --fix    # réaligne tout sur VERSION

`--fix` écrit en mode binaire et ne remplace que la sous-chaîne de version :
les fins de ligne CRLF de la moitié des fichiers du dépôt sont préservées telles
quelles (une réécriture texte produirait un diff de plusieurs centaines de
lignes pour un changement de trois caractères).
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (chemin, regex avec UN groupe capturant la version, libellé)
# La regex doit matcher la déclaration COURANTE, pas les rappels historiques
# ("récupéré manuellement en v2.9.5" dans le README raconte le passé : il ne
# doit surtout pas être réaligné).
SITES = [
    ("VERSION",                                   r"^\s*(\d+\.\d+\.\d+)\s*$",                    "fichier VERSION (référence)"),
    # NB : $script:AgentVersion (Scripts/SysAdminMaster-Agent.ps1) est VOLONTAIREMENT
    # EXCLU de cette liste depuis le 31/07/2026. Ce n'est pas une bannière doc : c'est
    # la version RUNTIME que l'UI /agents compare au heartbeat de chaque hôte pour
    # décider s'il est "legacy". La forcer à suivre VERSION a fait apparaître 5 bumps
    # docs-only (2.10.5→2.12.0) sans aucun changement de ce fichier, ce qui a rendu
    # TOUS les agents faussement "legacy" (y compris un hôte tout juste récupéré,
    # resté à 2.10.4 = son vrai dernier contenu fonctionnel). Cette version
    # ne doit être bumpée qu'à la main, en même temps qu'un changement réel du script.
    ("Scripts/Install-SysAdminMaster-Agent.ps1",  r"Installe l'agent SysAdminMaster - Version (\d+\.\d+\.\d+)", "en-tête de l'installeur"),
    ("Scripts/Install-SysAdminMaster-Agent.ps1",  r"Agent Installer v(\d+\.\d+\.\d+)",           "bannière de l'installeur"),
    ("package.json",                              r'"version"\s*:\s*"(\d+\.\d+\.\d+)"',          "package.json (dépendance e2e)"),
]


def read_text(path: pathlib.Path) -> str:
    return path.read_bytes().decode("utf-8")


def main() -> int:
    fix = "--fix" in sys.argv

    version_file = ROOT / "VERSION"
    if not version_file.exists():
        print("::error::fichier VERSION absent à la racine du dépôt")
        return 1
    expected = read_text(version_file).strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", expected):
        print(f"::error::VERSION ne contient pas un semver valide : {expected!r}")
        return 1

    print(f"Référence : VERSION = {expected}\n")

    drift, missing, fixed = [], [], []

    for rel, pattern, label in SITES:
        path = ROOT / rel
        if not path.exists():
            missing.append((rel, label, "fichier absent"))
            continue

        text = read_text(path)
        m = re.search(pattern, text, re.MULTILINE)
        if not m:
            # Une déclaration qui disparaît est aussi une dérive : soit le
            # fichier a changé de forme, soit la liste SITES est périmée.
            missing.append((rel, label, "déclaration introuvable (motif obsolète ?)"))
            continue

        found = m.group(1)
        if found == expected:
            print(f"  ok      {rel:<44} {label}")
            continue

        if fix:
            raw = path.read_bytes()
            old_decl = m.group(0)
            new_decl = old_decl.replace(found, expected)
            path.write_bytes(raw.replace(old_decl.encode("utf-8"), new_decl.encode("utf-8"), 1))
            fixed.append((rel, label, found))
            print(f"  corrigé {rel:<44} {label} : {found} -> {expected}")
        else:
            drift.append((rel, label, found))
            print(f"  DÉRIVE  {rel:<44} {label} : {found} (attendu {expected})")

    print()

    if missing:
        for rel, label, why in missing:
            print(f"::error file={rel}::{label} — {why}")
        print(f"\n{len(missing)} site(s) de version illisible(s). "
              "Corriger le fichier, ou mettre à jour SITES dans tools/check-version.py.")
        return 1

    if fixed:
        print(f"{len(fixed)} déclaration(s) réalignée(s) sur {expected}.")
        return 0

    if drift:
        for rel, label, found in drift:
            print(f"::error file={rel}::{label} déclare {found}, VERSION dit {expected}")
        print(f"\n{len(drift)} déclaration(s) désynchronisée(s). "
              "Rejouer avec --fix, puis recommiter.\n"
              "Le projet a déjà vécu avec 11 versions divergentes : on ne recommence pas.")
        return 1

    print(f"Toutes les déclarations sont alignées sur {expected}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
