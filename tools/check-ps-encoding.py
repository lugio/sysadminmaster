#!/usr/bin/env python3
"""Refuse les caracteres typographiques Unicode dans le CODE PowerShell.

POURQUOI CE GARDE-FOU EXISTE
----------------------------
Les scripts agent sont servis en UTF-8 par l'AgentMaster puis executes par
PowerShell 5.1, qui les relit en CP1252. Un tiret cadratin U+2014 (octets UTF-8
E2 80 94) devient alors la sequence CP1252 `a€"` dont le dernier caractere est
U+201D -- que PowerShell accepte comme DELIMITEUR DE CHAINE. La chaine se termine
donc trop tot et le script ne compile plus.

Trois incidents sur ce seul mecanisme :
  - v2.9.5 (23/06/2026) : cadratin dans une chaine, parse error.
  - 31/07/2026 : `'Telechargement'` litteral illisible en CP1252 -> 0 resultat.
  - 31/07/2026 (2.24.1) : cadratin dans le message de `restart_agent`. Le
    parse-check embarque de l'agent l'a refuse et a preserve la flotte -- alors
    que ma verification manuelle avait annonce OK (elle reecrivait le fichier en
    UTF-8 avec BOM avant de le parser, ce qui masque precisement ce bug).

CE QU'IL VERIFIE, ET CE QU'IL NE VERIFIE PAS
--------------------------------------------
Seul le CODE est controle. Les commentaires sont EXCLUS, et ce n'est pas une
approximation : un commentaire est consomme jusqu'a sa fin quel que soit son
contenu. Preuve empirique du 31/07 -- 15 cadratins en commentaire, zero erreur ;
1 cadratin en chaine, 1 erreur. Interdire les accents en commentaire rendrait la
doc du code illisible pour un gain nul.

Ce controle ne remplace pas le parse-check runtime sur hote reel (Iron Law 5) :
il attrape une cause connue, pas toutes les causes.
"""
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Caracteres que PowerShell 5.1 traite comme delimiteurs apres relecture CP1252,
# ou qui cassent une chaine. Les lettres accentuees (e, a, e...) ne sont PAS
# listees : elles ont un equivalent CP1252 sur un seul octet et traversent sans
# dommage -- c'est verifie, la flotte tourne avec depuis des mois.
DANGEROUS = {
    0x2014: "tiret cadratin —",
    0x2013: "tiret demi-cadratin –",
    0x201C: "guillemet double ouvrant “",
    0x201D: "guillemet double fermant ”",
    0x2018: "apostrophe ouvrante ‘",
    0x2019: "apostrophe fermante ’",
    0x00AB: "guillemet francais ouvrant «",
    0x00BB: "guillemet francais fermant »",
}


def strip_comments(text):
    """Remplace les commentaires par des espaces, en preservant les positions.

    Suit l'etat des chaines : un `#` a l'interieur d'une chaine n'ouvre pas un
    commentaire. Sans ca, une ligne du genre `$url = "http://h/a#b"` verrait sa
    fin masquee et le controle deviendrait aveugle -- un faux negatif silencieux
    est pire que pas de controle.
    """
    out = list(text)
    i, n = 0, len(text)
    in_s = in_d = False          # chaine simple / double
    block = 0                    # profondeur de <# ... #>
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ''
        if block:
            if c == '#' and nxt == '>':
                block -= 1
                out[i] = out[i + 1] = ' '
                i += 2
                continue
            if c != '\n':
                out[i] = ' '
            i += 1
            continue
        if in_s:
            if c == "'":
                in_s = False
            i += 1
            continue
        if in_d:
            if c == '`':
                i += 2
                continue
            if c == '"':
                in_d = False
            i += 1
            continue
        if c == "'":
            in_s = True
        elif c == '"':
            in_d = True
        elif c == '<' and nxt == '#':
            block += 1
            out[i] = out[i + 1] = ' '
            i += 2
            continue
        elif c == '#':
            while i < n and text[i] != '\n':
                out[i] = ' '
                i += 1
            continue
        i += 1
    return ''.join(out)


def main():
    files = sorted(set(ROOT.glob('**/*.ps1')) | set(ROOT.glob('**/*.psm1')))
    files = [f for f in files if '.git' not in f.parts and 'node_modules' not in f.parts]

    problems = []
    for f in files:
        try:
            raw = io.open(f, encoding='utf-8').read()
        except UnicodeDecodeError:
            problems.append((f, 0, "fichier non decodable en UTF-8"))
            continue
        code = strip_comments(raw)
        for lineno, line in enumerate(code.split('\n'), 1):
            for ch in line:
                if ord(ch) in DANGEROUS:
                    src = raw.split('\n')[lineno - 1].strip()
                    problems.append((f, lineno, f"{DANGEROUS[ord(ch)]} -> {src[:90]}"))
                    break

    rel = lambda p: p.relative_to(ROOT)
    if problems:
        print(f"\n❌ {len(problems)} caractere(s) typographique(s) dans du code PowerShell :\n")
        for f, ln, msg in problems:
            print(f"   {rel(f)}:{ln}  {msg}")
        print("\n   PowerShell 5.1 relit ces scripts en CP1252 : ces caracteres y deviennent")
        print("   des delimiteurs de chaine et cassent le parsing. Utiliser l'ASCII"
              " equivalent")
        print("   (- pour un cadratin, \" et ' droits). Les commentaires ne sont pas concernes.\n")
        return 1

    print(f"✅ {len(files)} script(s) PowerShell : aucun caractere typographique dans le code.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
