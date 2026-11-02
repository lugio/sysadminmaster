"""Vue « état des jetons Office » — SOURCE UNIQUE, partagée par l'alerte et l'API.

Pourquoi ce module existe
-------------------------
Le 01/09/2026, la page `/licenses` annonçait **2 jetons périmés** pendant que le
mail quotidien en comptait **29**. Les deux disaient vrai dans leur référentiel :
la page lisait le **dernier snapshot de chaque hôte**, le mail le **dernier état
connu de chaque utilisateur**. Or sur une ferme en profils UPD, le dernier
snapshot d'un hôte ne contient que les gens connectés à cet instant — le soir,
personne.

Deux chiffres pour la même question, dans le même outil, c'est l'outil qui perd
sa crédibilité. Le calcul vit donc ici, et une seule fois.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_DIR, 'agentmaster.db')

# Horizon d'observation : au-dela, on considere le compte parti (aligne sur le
# filtre `inactiveDays` de l'agent).
OBSERVATION_HORIZON_DAYS = int(os.environ.get('SAM_ALERT_HORIZON_DAYS', '45'))
# Duree de validite d'un jeton SCA. Doit rester aligne sur $staleDays de l'agent.
STALE_DAYS = int(os.environ.get('SAM_ALERT_STALE_DAYS', '30'))


_DN_CACHE = None


def _dn_cache():
    """Cache serveur des DN resolus a la demande (Web/core/dn_cache).

    Charge une fois par processus : la table est petite et evolue rarement.
    Ne leve jamais — un cache absent n'est pas une panne, seulement moins
    d'information.
    """
    global _DN_CACHE
    if _DN_CACHE is None:
        try:
            import sys
            sys.path.insert(0, REPO_DIR)
            from Web.core.dn_cache import load
            _DN_CACHE = load()
        except Exception:
            _DN_CACHE = {}
    return _DN_CACHE


def load_summary():
    """Dernier état connu de CHAQUE utilisateur, agrégé sur tout l'historique.

    ⚠️ Ne PAS revenir à « le dernier snapshot de chaque hôte » (implémentation
    d'origine). Sur une ferme en profils UPD, l'agent ne voit que les
    utilisateurs connectés : le dernier snapshot d'un hôte le soir contient zéro
    utilisateur, et l'alerte concluait « aucun jeton périmé ». Mesuré le
    31/08/2026 : 8 utilisateurs visibles dans les derniers snapshots contre 324
    connus, et 3 jetons périmés signalés contre 32 réels.

    Chaque utilisateur porte sa date d'observation (`observed_at`) : ici les
    dates s'étalent sur plusieurs jours par construction, et un chiffre sans sa
    date d'observation ment par omission.

    Retourne la même forme que la version précédente — `[{hostname, sca_status,
    office_version, recorded_at, users:[...]}]` — pour que `build_digest` et
    `_counts` restent inchangés.
    """
    horizon = datetime.now() - timedelta(days=OBSERVATION_HORIZON_DAYS)
    hosts = {}
    with sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row

        # Métadonnées d'hôte (SCA, version Office) : là, le dernier snapshot est
        # le bon niveau — ce sont des propriétés de la machine, pas des users.
        meta_sql = (
            "SELECT h.hostname, h.recorded_at, h.sca_status, h.office_version "
            "FROM license_history h INNER JOIN ("
            "  SELECT hostname, MAX(recorded_at) AS max_ts FROM license_history GROUP BY hostname"
            ") latest ON h.hostname = latest.hostname AND h.recorded_at = latest.max_ts"
        )
        for r in conn.execute(meta_sql):
            hosts[r['hostname']] = {'hostname': r['hostname'],
                                    'recorded_at': r['recorded_at'],
                                    'sca_status': r['sca_status'],
                                    'office_version': r['office_version'],
                                    'users': []}

        # État par utilisateur : parcours en ordre croissant, la dernière
        # observation écrase les précédentes.
        users_sql = ("SELECT hostname, recorded_at, users_data FROM license_history "
                     "WHERE recorded_at >= ? ORDER BY recorded_at ASC")
        # 🔴 Le DN est une propriete du COMPTE, pas de l'observation. Les lignes
        # anterieures a l'agent 2.29.0 n'en portent pas : chercher le DN dans la
        # seule derniere observation laisserait le perimetre inoperant pendant
        # des semaines. On construit donc un index nom -> dernier DN connu, tous
        # hotes et toutes dates confondus.
        dn_by_name = {}
        seen = {}
        # 🔴 Une observation plus RECENTE sur un AUTRE hote remplace l'ancienne.
        # Le profil est en UPD : un seul disque, monte sur l'hote du moment.
        # Le dossier Licensing vu sur un hote A le 03/09 est LE MEME que celui
        # purge sur un hote B le 08/09. Garder l'ancienne ligne par (hote, user)
        # faisait revenir un jeton PERIME (82 j, projete) juste apres une purge
        # verifiee — trois reparations « reussies » de suite le 08/09 sur
        # un meme utilisateur, la page redisant a chaque fois « périmé » depuis le
        # fantome de l'hote A. Et l'alerte, qui garde le pire age par personne,
        # aurait relance pendant 45 jours (horizon) un jeton qui n'existe plus.
        latest_by_name = {}
        hosts_by_name = {}
        for r in conn.execute(users_sql, (horizon.isoformat(),)):
            try:
                users = json.loads(r['users_data'] or '[]')
            except (ValueError, TypeError):
                continue
            for u in users:
                name = u.get('name')
                if not name:
                    continue
                if u.get('dn'):
                    dn_by_name[name.lower()] = u['dn']
                seen[(r['hostname'], name)] = dict(u, observed_at=r['recorded_at'])
                latest_by_name[name.lower()] = (r['hostname'], r['recorded_at'])
                hosts_by_name.setdefault(name.lower(), set()).add(r['hostname'])

    now = datetime.now()
    for (hostname, _name), user in seen.items():
        host = hosts.get(hostname)
        if host is None:
            continue
        # Observation supplantee par une plus recente ailleurs : on ne la sert
        # pas (ni page, ni alerte). Les hotes ou la personne a ete vue restent
        # cites sur la ligne survivante (`seen_on_hosts`) pour ne rien cacher.
        cle = _name.lower()
        if latest_by_name.get(cle, (hostname, user.get('observed_at')))[0] != hostname:
            continue
        user['seen_on_hosts'] = sorted(hosts_by_name.get(cle, {hostname}))

        # 🔴 AGE PROJETE — sans lui, un retour de conges reste indetectable.
        # Un jeton ne rajeunit pas tout seul : s'il etait a 10 jours quand on l'a
        # vu il y a 28 jours, il en a 38 aujourd'hui, donc il est PERIME. C'est
        # exactement le cas du 31/08 : 4 semaines d'absence, jamais scanne pendant
        # ce temps (profil UPD demonte), et l'alerte n'avait rien a dire le matin
        # de son retour. On projette, et on le DIT (`age_projected`) pour ne pas
        # faire passer un calcul pour une mesure.
        if not user.get('dn'):
            # 1) DN vu dans l'historique, 2) cache serveur resolu a la demande.
            # Le cache DOIT etre consulte ICI et pas chez l'appelant : c'est la
            # source commune de la page et du mail. Le consulter d'un seul cote
            # recreerait la divergence corrigee en 2.29.1 (constate : la page
            # ecartait deja un client et un depart que le mail listait encore).
            cle = (user.get('name') or '').lower()
            user['dn'] = dn_by_name.get(cle) or _dn_cache().get(cle)
        try:
            gap = (now - datetime.fromisoformat(user['observed_at'])).total_seconds() / 86400.0
        except (KeyError, TypeError, ValueError):
            gap = 0.0
        user['observed_days_ago'] = round(gap, 1)
        if user.get('age_days') is not None:
            user['age_measured'] = user['age_days']
            user['age_days'] = round(user['age_days'] + gap, 1)
            user['age_projected'] = gap >= 1.0
            # Un jeton mesure 'ok' peut avoir franchi les 30 jours depuis.
            if user.get('status') == 'ok' and user['age_days'] > STALE_DAYS:
                user['status'] = 'stale'
                user['became_stale_by_projection'] = True
        host['users'].append(user)
    return list(hosts.values())


