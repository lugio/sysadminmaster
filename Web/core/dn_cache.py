"""Cache serveur des DN AD, alimenté à la demande par un agent.

Pourquoi
--------
Le DN d'un profil n'arrive qu'au moment où un agent 2.29.0+ le scanne, donc au
moment où la personne ouvre une session. Un compte rarement connecté (client,
départ, compte de service) resterait « périmètre indéterminé » indéfiniment et
continuerait de polluer la vue « Utilisateurs Office 365 ».

On demande donc à un agent — qui tourne en SYSTEM sur une machine du domaine,
donc **sans aucun identifiant** — de résoudre les noms manquants, et on garde le
résultat ici. Un `dn = None` est mémorisé lui aussi : « inconnu de l'AD » est une
réponse, pas un échec, et sans ça on relancerait la recherche indéfiniment.
"""
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'agentmaster.db')

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS user_dn_cache (
           name        TEXT PRIMARY KEY,
           dn          TEXT,
           resolved_at TIMESTAMP NOT NULL
       )""",
]


def _conn(readonly=False):
    if readonly:
        return sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    for stmt in SCHEMA:
        c.execute(stmt)
    return c


def load():
    """{nom_minuscule: dn or None}. Ne lève jamais : un cache absent n'est pas une panne."""
    try:
        with _conn(readonly=True) as c:
            return {r[0].lower(): r[1] for r in c.execute('SELECT name, dn FROM user_dn_cache')}
    except Exception:
        return {}


def store(mapping):
    """Enregistre {nom: dn|None}. Retourne le nombre de lignes écrites."""
    n = 0
    with _conn() as c:
        for name, dn in mapping.items():
            c.execute("""INSERT INTO user_dn_cache (name, dn, resolved_at) VALUES (?,?,?)
                         ON CONFLICT(name) DO UPDATE SET dn=excluded.dn, resolved_at=excluded.resolved_at""",
                      (name, dn or None, datetime.now().isoformat(timespec='seconds')))
            n += 1
        c.commit()
    return n
