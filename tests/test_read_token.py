"""Jeton de lecture seule (2.32.0) : il LIT les routes de consultation, il ne COMMANDE jamais.
Lancement : .venv-linux/bin/python tests/test_read_token.py  (copie isolée : aucune base réelle touchée)."""
import importlib.util, os, shutil, sys, tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def charger(env):
    t = tempfile.mkdtemp(prefix="sam-test-")
    shutil.copytree(os.path.join(RACINE, "AgentMaster"), os.path.join(t, "AgentMaster"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    for k in ("SAM_AGENT_TOKEN", "SAM_MASTER_TOKEN", "SAM_READ_TOKEN", "SAM_AGENT_TOKEN_LEGACY", "SAM_ALERT_TO"):
        os.environ.pop(k, None)
    os.environ.update(env)
    sys.path.insert(0, os.path.join(t, "AgentMaster"))
    spec = importlib.util.spec_from_file_location(f"master_{len(os.listdir(tempfile.gettempdir()))}",
                                                  os.path.join(t, "AgentMaster", "master.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m, t

ok = 0
def verifier(cond, msg):
    global ok
    if not cond:
        sys.exit("ÉCHEC : " + msg)
    ok += 1; print("ok  ", msg)

m, t = charger({"SAM_AGENT_TOKEN": "a-test", "SAM_MASTER_TOKEN": "m-test", "SAM_READ_TOKEN": "l-test"})
c = m.app.test_client()
L, M, X = {"X-Read-Token": "l-test"}, {"X-Master-Token": "m-test"}, {"X-Read-Token": "faux"}
for r in ("/api/master/agents", "/api/master/history?hostname=RDSH01", "/api/master/alerts/status",
          "/api/master/alerts/history", "/api/master/licenses"):
    verifier(c.get(r, headers=L).status_code != 401, f"lecture seule LIT {r.split('?')[0]}")
    verifier(c.get(r, headers=M).status_code != 401, f"master lit toujours {r.split('?')[0]}")
    verifier(c.get(r, headers=X).status_code == 401, f"faux jeton refusé sur {r.split('?')[0]}")
    verifier(c.get(r).status_code == 401, f"sans jeton refusé sur {r.split('?')[0]}")
corps = {"hostname": "RDSH01", "action": "logoff", "params": {}}
verifier(c.post("/api/master/command", json=corps, headers=L).status_code == 401, "lecture seule NE COMMANDE PAS (command)")
verifier(c.post("/api/master/command", json=corps, headers={"X-Master-Token": "l-test"}).status_code == 401,
         "jeton lecture passé en X-Master-Token refusé")
verifier(c.get("/api/master/command-status/x", headers=L).status_code == 401, "lecture seule refusée sur command-status")
for r in ("/api/master/alerts/test", "/api/master/alerts/refresh-delivery", "/api/master/agent-deploy"):
    verifier(c.post(r, json={}, headers=L).status_code == 401, f"lecture seule refusée sur POST {r}")
verifier(c.post("/api/master/command", json=corps, headers=M).status_code == 200, "master commande toujours")
shutil.rmtree(t, ignore_errors=True)

m2, t2 = charger({"SAM_AGENT_TOKEN": "a-test", "SAM_MASTER_TOKEN": "m-test"})
verifier(m2.app.test_client().get("/api/master/agents", headers={"X-Read-Token": ""}).status_code == 401,
         "sans SAM_READ_TOKEN : aucune lecture par jeton vide")
shutil.rmtree(t2, ignore_errors=True)

try:
    charger({"SAM_AGENT_TOKEN": "a-test", "SAM_MASTER_TOKEN": "m-test", "SAM_READ_TOKEN": "m-test"})
    verifier(False, "jeton lecture = master doit être refusé au démarrage")
except SystemExit as e:
    verifier("identique" in str(e), "jeton lecture = master : refus de démarrer")
print(f"\n{ok} vérifications, toutes vertes")
