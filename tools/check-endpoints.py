#!/usr/bin/env python3
"""
Garde-fou d'alignement ENDPOINTS ↔ APPELS UI.

Pourquoi ce script existe
-------------------------
Le projet n'a pas de description OpenAPI : rien ne
décrit le contrat des 46 routes Flask. Les bugs du 31/07/2026 viennent tous de
cette zone grise — un template appelait `#sysinfo-content-card` qui n'existait
pas, `/api/services` renvoyait deux formes de payload selon la branche, et
`api.py` lisait des clés `os_caption` inexistantes.

Ce script ne remplace pas une doc d'API : il vérifie mécaniquement les deux
désalignements qui produisent des pannes silencieuses :

  A. l'UI appelle une route qui n'existe pas          -> 404 à l'exécution
  B. l'UI appelle une route d'un blueprint NON monté  -> 404 aussi, plus vicieux
                                                         (la route existe dans le
                                                          code, on la croit vivante)

Il liste aussi (informatif) les routes jamais appelées par l'UI : soit de l'API
pure légitime, soit du code mort.

Usage
-----
    python3 tools/check-endpoints.py          # rapport, code 1 si défaut A ou B
    python3 tools/check-endpoints.py --list   # inventaire complet des routes
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES_DIR = ROOT / 'Web' / 'routes'
TPL_DIR = ROOT / 'Web' / 'templates'

# Blueprints réellement montés dans Web/app.py — un fichier de routes non
# enregistré est du code mort qui RESSEMBLE à du code vivant.
APP_PY = (ROOT / 'Web' / 'app.py').read_text(encoding='utf-8')
REGISTERED_BP = set(re.findall(r'register_blueprint\(\s*(\w+)', APP_PY))

ROUTE_RE = re.compile(r"@(\w+)\.route\(\s*['\"]([^'\"]+)['\"]")
# Appels UI : fetch('...'), fetch(`...${x}`), href="/api/..."
FETCH_RE = re.compile(r"fetch\(\s*['\"`]([^'\"`$]*)")
HREF_RE = re.compile(r"""href=['\"]\s*(/api/[^'\"?]+)""")


def load_routes():
    """{path: {'bp','file','live'}} — `live` = blueprint monté dans app.py."""
    routes = {}
    for f in sorted(ROUTES_DIR.glob('*.py')):
        for bp, path in ROUTE_RE.findall(f.read_text(encoding='utf-8')):
            routes[path] = {'bp': bp, 'file': f.name, 'live': bp in REGISTERED_BP}
    return routes


def load_calls():
    """{url_sans_querystring: {templates}}"""
    calls = {}
    for f in sorted(TPL_DIR.glob('*.html')):
        src = f.read_text(encoding='utf-8')
        for raw in FETCH_RE.findall(src) + HREF_RE.findall(src):
            url = raw.split('?')[0].rstrip('/')
            if url.startswith('/'):
                calls.setdefault(url, set()).add(f.name)
    return calls


def route_matches(call, route):
    """Un appel correspond-il à une route (avec ses <params>) ?

    ⚠️ Les appels sont TRONQUÉS à la 1re interpolation JS : `/api/reports/${type}`
    est extrait comme `/api/reports`. Sans en tenir compte, l'outil crie au 404
    sur des endpoints parfaitement sains — un faux positif coûte plus cher que
    pas d'audit du tout, parce qu'il envoie chercher un bug qui n'existe pas.
    On accepte donc aussi qu'un appel soit le PRÉFIXE d'une route, à une
    frontière de segment (`/api/reports` couvre `/api/reports/health`).
    """
    route = route.rstrip('/')
    if re.match('^' + re.sub(r'<[^>]+>', '[^/]+', route) + '$', call):
        return True
    return route.startswith(call + '/')


def main():
    routes, calls = load_routes(), load_calls()

    if '--list' in sys.argv:
        for path, meta in sorted(routes.items()):
            print(f"  {'live' if meta['live'] else 'MORT'}  {path:45s} {meta['file']}")
        return 0

    missing, dead = [], []
    for call, files in sorted(calls.items()):
        hits = [r for r in routes if route_matches(call, r)]
        if not hits:
            missing.append((call, files))
        elif not any(routes[r]['live'] for r in hits):
            dead.append((call, files, routes[hits[0]]['file']))

    print(f"Routes déclarées : {len(routes)}  |  appels UI distincts : {len(calls)}")
    print(f"Blueprints montés : {', '.join(sorted(REGISTERED_BP)) or '(aucun)'}\n")

    if missing:
        print("❌ APPELS UI SANS ROUTE (404 à l'exécution) :")
        for call, files in missing:
            print(f"     {call:45s} <- {', '.join(sorted(files))}")
    else:
        print("✅ Tout appel de l'UI a une route déclarée.")

    if dead:
        print("\n❌ APPELS VERS UN BLUEPRINT NON MONTÉ (route présente mais morte) :")
        for call, files, src in dead:
            print(f"     {call:45s} <- {', '.join(sorted(files))}  [{src}]")
    else:
        print("✅ Aucun appel ne vise un blueprint non monté.")

    unused = [(p, m) for p, m in sorted(routes.items())
              if p.startswith('/api') and not any(route_matches(c, p) for c in calls)]
    if unused:
        print(f"\nℹ️  {len(unused)} route(s) /api jamais appelée(s) par l'UI "
              "(API pure ou code mort — à trier, pas un défaut en soi) :")
        for p, m in unused:
            print(f"     {p:45s} {m['file']}{'' if m['live'] else '  💀 blueprint non monté'}")

    return 1 if (missing or dead) else 0


if __name__ == '__main__':
    sys.exit(main())
