#!/usr/bin/env python3
"""Verifie la syntaxe du JavaScript INLINE des templates Jinja.

Raison d'etre (23/09/2026) : une apostrophe non echappee dans une chaine JS de
`licenses.html` a tue TOUT le bloc <script> de la page. Consequence : plus aucun
bouton rendu, et DEUX specs e2e en echec sur un symptome (`waitForSelector`
timeout) qui ne designait pas la cause. Aucun garde-fou du projet ne couvrait ce
cas : `check-endpoints.py` regarde les URL appelees, la CI parse le Python et le
PowerShell -- personne ne parsait le JS des templates, qui est pourtant le seul
code du projet a n'etre compile par rien avant d'atteindre le navigateur.

Une erreur de syntaxe JS est silencieuse cote serveur : la page est servie en
200, le HTML est complet, et seul le navigateur constate que rien ne s'execute.

Sortie : 0 si tout compile, 1 sinon. Utilise par le hook pre-push et la CI.
"""
import io
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, 'Web', 'templates')

# <script> sans attribut src : uniquement le code ecrit dans le template.
SCRIPT_RE = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S | re.I)


def neutralize_jinja(js):
    """Remplace les expressions Jinja par des litteraux JS valides.

    `{{ x }}` -> `null` (valide en valeur comme dans une chaine : '{{x}}' -> 'null')
    `{% ... %}` -> supprime (le JS qu'il entoure reste, et c'est ce qu'on veut parser)
    """
    js = re.sub(r'\{\{.*?\}\}', 'null', js, flags=re.S)
    js = re.sub(r'\{%.*?%\}', '', js, flags=re.S)
    return js


def check_file(path):
    src = io.open(path, encoding='utf-8').read()
    blocks = SCRIPT_RE.findall(src)
    if not blocks:
        return []
    js = neutralize_jinja('\n;\n'.join(blocks))
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(js)
        tmp = fh.name
    try:
        proc = subprocess.run(['node', '--check', tmp],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return []
        # Ne garder que les lignes utiles de la sortie de node.
        detail = [l for l in (proc.stderr or '').splitlines()
                  if l.strip() and not l.startswith('    at ')][:6]
        return [(path, detail)]
    finally:
        os.unlink(tmp)


def main():
    if subprocess.run(['which', 'node'], capture_output=True).returncode != 0:
        print('node introuvable - controle ignore (non bloquant)')
        return 0
    if not os.path.isdir(TPL_DIR):
        print('Aucun dossier de templates - rien a verifier')
        return 0

    failures = []
    checked = 0
    for name in sorted(os.listdir(TPL_DIR)):
        if not name.endswith('.html'):
            continue
        path = os.path.join(TPL_DIR, name)
        checked += 1
        failures.extend(check_file(path))

    if failures:
        print('[ERREUR] JavaScript inline invalide dans %d template(s) :\n'
              % len(failures))
        for path, detail in failures:
            print('  %s' % os.path.relpath(path, ROOT))
            for line in detail:
                print('      %s' % line)
        print('\nUne erreur de syntaxe JS ne leve RIEN cote serveur : la page est')
        print('servie en 200 et seul le navigateur voit que le script est mort.')
        return 1

    print('[OK] %d template(s) : le JavaScript inline compile.' % checked)
    return 0


if __name__ == '__main__':
    sys.exit(main())
