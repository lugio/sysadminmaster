#!/usr/bin/env python3
"""Résout les DN AD manquants en demandant a un agent en ligne, et les met en cache.

Usage : ./.venv-linux/bin/python tools/resolve-dns.py
L'agent tourne en SYSTEM sur une machine du domaine : aucune identification
n'est requise, et le compte de bind AD (admin du domaine) n'est jamais diffuse.
"""
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Web.core.dn_cache import load, store
from Web.core.o365_scope import is_technical

MASTER = 'http://127.0.0.1:5000'
TOKEN = os.environ.get('SAM_MASTER_TOKEN', '')


def _post(path, payload):
    req = urllib.request.Request(MASTER + path, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json', 'X-Master-Token': TOKEN})
    return json.load(urllib.request.urlopen(req, timeout=20))


def _get(path):
    req = urllib.request.Request(MASTER + path, headers={'X-Master-Token': TOKEN})
    return json.load(urllib.request.urlopen(req, timeout=20))


def main():
    from AgentMaster.license_view import load_summary
    connus = load()
    manquants = sorted({
        u['name'] for h in load_summary() for u in (h.get('users') or [])
        if u.get('name') and not u.get('dn')
        and u['name'].lower() not in connus and not is_technical(u['name'])
    })
    if not manquants:
        print("Aucun DN a resoudre.")
        return 0
    print(f"{len(manquants)} nom(s) a resoudre : {', '.join(manquants[:10])}"
          + (' …' if len(manquants) > 10 else ''))

    hote = next((h for h, v in _get('/api/master/agents').items()
                 if str(v.get('agent_version', '')) >= '2.29.0'), None)
    if not hote:
        print("Aucun agent 2.29.0+ disponible.")
        return 1

    ps = ("$noms = @(" + ",".join(f"'{n}'" for n in manquants) + ")\n"
          "foreach ($n in $noms) {\n"
          "  $dn = ''\n"
          "  try {\n"
          "    $s = New-Object System.DirectoryServices.DirectorySearcher\n"
          "    $s.Filter = \"(&(objectCategory=person)(objectClass=user)(sAMAccountName=$n))\"\n"
          "    [void]$s.PropertiesToLoad.Add('distinguishedName')\n"
          "    $f = $s.FindOne()\n"
          "    if ($f) { $dn = [string]$f.Properties['distinguishedname'][0] }\n"
          "  } catch { }\n"
          "  \"$n|$dn\"\n"
          "}\n")
    cmd = _post('/api/master/command', {'hostname': hote, 'action': 'run_ps_command',
                                        'params': {'command': ps}})['commandId']
    for _ in range(20):
        time.sleep(6)
        st = _get(f'/api/master/command-status/{cmd}')
        if st.get('status') in ('success', 'error'):
            break
    else:
        print("Delai depasse.")
        return 1
    if st.get('status') != 'success':
        print("Echec :", st.get('result_message'))
        return 1

    mapping = {}
    for ligne in (st.get('result_message') or '').splitlines():
        if '|' in ligne:
            nom, dn = ligne.split('|', 1)
            mapping[nom.strip()] = dn.strip() or None
    store(mapping)
    resolus = sum(1 for v in mapping.values() if v)
    print(f"{len(mapping)} nom(s) mis en cache depuis {hote} — {resolus} trouve(s) dans l'AD, "
          f"{len(mapping) - resolus} inconnu(s) (comptes locaux).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
