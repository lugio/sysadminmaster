"""
Alerte quotidienne « jetons Office 365 » — passe de réactif à préventif.

Pourquoi ce module existe
-------------------------
Les plaintes utilisateurs sur Office viennent de jetons SCA qui vieillissent en
silence : personne ne regarde la page /licenses tous les jours, on découvre le
jeton périmé quand l'utilisateur est déjà bloqué et appelle le support. Ce module
inverse le sens : un contrôle quotidien envoie un mail dès qu'un jeton dépasse
30 jours (statut `stale`) ou qu'un serveur a la SCA désactivée — pour réparer
AVANT l'appel.

Conception
----------
- Zéro dépendance : Mailjet en REST via urllib (le venv n'a pas `requests`).
- Ne DOIT jamais faire tomber l'ingestion : la boucle est intégralement
  enveloppée, une erreur d'alerte se loggue et n'atteint jamais master.py.
- Un seul mail par jour maximum (état persisté dans .alert-state.json, survit
  aux redémarrages fréquents du service — Restart=always).
- Désactivé par défaut : sans SAM_ALERT_TO ou sans SAM_ALERT_FROM dans
  l'environnement, il ne fait rien (loggé une fois). Les clés Mailjet vivent dans
  .env (chmod 600, gitignoré) — jamais dans le dépôt.

Usage manuel (test / envoi forcé, sans attendre l'heure du contrôle) :
    cd <install-dir> && set -a; . ./.env; set +a
    ./.venv-linux/bin/python -m AgentMaster.license_alerts --dry-run   # affiche sans envoyer
    ./.venv-linux/bin/python -m AgentMaster.license_alerts --force    # envoie même sans anomalie
"""

import base64
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta

logger = logging.getLogger("AgentMaster")

# La base vit à la RACINE du repo (master.py la crée depuis son propre BASE_DIR,
# qui est le répertoire de travail du service), pas dans AgentMaster/.
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_DIR, 'agentmaster.db')
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.alert-state.json')

ALERT_HOUR = int(os.environ.get('SAM_ALERT_HOUR', '7'))          # heure locale du contrôle
try:
    from Web.core import settings as _settings
except ImportError:
    import sys as _sys
    _sys.path.insert(0, REPO_DIR)
    try:
        from Web.core import settings as _settings
    except ImportError:
        # AgentMaster copied without Web/ (isolated tests, minimal install): read the
        # same variables from the environment only, so the service still starts.
        class _settings:
            @staticmethod
            def dashboard_url(path=''):
                base = os.environ.get('SAM_DASHBOARD_URL', '').strip().rstrip('/')
                return (base + path) if base else ''

            @staticmethod
            def o365_scope_ou():
                return os.environ.get('SAM_O365_SCOPE_OU', '').strip()

            @staticmethod
            def o365_scope_exclude():
                return [x.strip() for x in os.environ.get('SAM_O365_SCOPE_EXCLUDE', '').split(',') if x.strip()]

ALERT_FROM = os.environ.get('SAM_ALERT_FROM', '').strip()        # obligatoire pour envoyer
DASHBOARD_URL = _settings.dashboard_url('/licenses')              # vide = pas de lien dans le mail

# Rétention de l'historique. 180 jours de contrôles quotidiens = ~180 lignes :
# sans commune mesure avec les 276 000 lignes qui avaient fait exploser
# `license_history`, mais on purge quand même — une table sans purge finit
# toujours par en devenir une.
HISTORY_RETENTION_DAYS = 180

# 🔴 Horizon d'observation d'un utilisateur.
# La ferme est en profils UPD : `C:\\Users\\<user>` n'existe QUE pendant la session
# de l'utilisateur (le VHDX est démonté à la déconnexion). L'agent scanne
# `C:\\Users` — il ne peut donc voir que les utilisateurs CONNECTÉS à cet instant.
# Lire « le dernier snapshot par hôte » revenait à ne contrôler que les quelques
# personnes en ligne à 7h00, et à lire une ferme vide comme une ferme saine :
# les 29 et 30/08/2026 (samedi, dimanche) ont été tracés « RAS — aucun jeton
# périmé » alors qu'un jeton de 487 jours dormait sur un hôte. On agrège donc le
# DERNIER ÉTAT CONNU de chaque utilisateur, borné à cet horizon — le même que le
# filtre `inactiveDays` de l'agent, pour ne pas alerter sur des comptes partis.
OBSERVATION_HORIZON_DAYS = int(os.environ.get('SAM_ALERT_HORIZON_DAYS', '45'))

# Duree de validite d'un jeton SCA. Doit rester aligne sur $staleDays de l'agent.
STALE_DAYS = int(os.environ.get('SAM_ALERT_STALE_DAYS', '30'))

# Perimetre des utilisateurs Office 365 (meme convention que Web/core/o365_scope).
SCOPE_OU = _settings.o365_scope_ou()            # vide = pas de filtre d'OU
SCOPE_EXCLUDE = _settings.o365_scope_exclude()

# Un mail qui liste 200 lignes n'est plus lu. Le détail complet vit sur le
# dashboard ; le mail donne le volume exact et les cas les plus anciens.
MAX_STALE_LISTED = 25

# 🔴 Source de vérité UNIQUE du schéma : `master.py` l'importe pour son
# `_init_db()` au lieu de redéclarer les colonnes. Le projet porte déjà une dette
# de ce type (`save_license_snapshot` dupliqué en deux exemplaires qui écrivent
# dans deux bases) — on ne la reproduit pas ici.
ALERT_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS alert_history (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        checked_at         TIMESTAMP NOT NULL,
        origin             TEXT,               -- 'daily' | 'manual'
        stale_count        INTEGER DEFAULT 0,
        sca_off_count      INTEGER DEFAULT 0,
        sent               INTEGER DEFAULT 0,  -- 0 = rien envoyé
        reason             TEXT,               -- pourquoi rien n'est parti
        recipients         TEXT,
        subject            TEXT,
        body               TEXT,
        mailjet_message_id TEXT,
        mailjet_status     TEXT,
        mailjet_checked_at TIMESTAMP,
        error              TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_alert_checked_at ON alert_history(checked_at)",
]

# Statuts Mailjet terminaux : inutile de les réinterroger.
MAILJET_FINAL = {'opened', 'clicked', 'bounce', 'hardbounced', 'softbounced',
                 'blocked', 'spam', 'unsub'}


def _connect(readonly=False):
    if readonly:
        return sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    return sqlite3.connect(DB_PATH, timeout=10)


def ensure_schema(conn):
    for stmt in ALERT_SCHEMA:
        conn.execute(stmt)


def record_run(origin, stale_count, sca_off_count, sent, reason=None,
               recipients=None, subject=None, body=None,
               mailjet_message_id=None, error=None):
    """Trace TOUT contrôle, y compris ceux qui n'envoient rien.

    C'est le point de la fonctionnalité : sans cette ligne, « rien reçu » et
    « tâche cassée » sont indiscernables — il a fallu une fouille dans l'API
    Mailjet pour trancher le 28/08/2026. Ne lève jamais : une erreur d'écriture
    d'historique ne doit pas empêcher une alerte de partir.
    """
    try:
        with _connect() as conn:
            ensure_schema(conn)
            conn.execute(
                """INSERT INTO alert_history
                   (checked_at, origin, stale_count, sca_off_count, sent, reason,
                    recipients, subject, body, mailjet_message_id, mailjet_status, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.now().isoformat(timespec='seconds'), origin,
                 stale_count, sca_off_count, 1 if sent else 0, reason,
                 ','.join(recipients or []), subject, body,
                 mailjet_message_id, 'queued' if mailjet_message_id else None, error))
            conn.execute(
                "DELETE FROM alert_history WHERE checked_at < datetime('now', ?)",
                ('-%d days' % HISTORY_RETENTION_DAYS,))
            conn.commit()
    except Exception as e:
        logger.warning(f"license_alerts: historique non enregistré ({e})")


def get_history(limit=60):
    try:
        with _connect(readonly=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM alert_history ORDER BY checked_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"license_alerts: historique illisible ({e})")
        return []


def refresh_delivery(limit=20):
    """Réinterroge Mailjet sur le sort réel des messages non encore terminaux.

    🔴 Raison d'être : le « success » rendu par l'API d'envoi ne prouve RIEN.
    Du 31/07 au 24/08/2026, les alertes partaient vers une adresse inexistante —
    hard bounce au 1er envoi puis exclusion permanente — pendant que le log
    affichait « mail envoyé » chaque jour. Seul le statut du MESSAGE fait foi.
    """
    api_key = os.environ.get('MAILJET_API_KEY', '')
    secret = os.environ.get('MAILJET_SECRET_KEY', '')
    if not api_key or not secret:
        return {"updated": 0, "checked": 0}
    try:
        with _connect(readonly=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, mailjet_message_id, mailjet_status FROM alert_history
                   WHERE mailjet_message_id IS NOT NULL AND mailjet_message_id != ''
                   ORDER BY checked_at DESC LIMIT ?""", (int(limit),)).fetchall()
    except Exception:
        return {"updated": 0, "checked": 0}

    auth = base64.b64encode(f"{api_key}:{secret}".encode()).decode()
    updated = checked = 0
    for r in rows:
        if (r['mailjet_status'] or '') in MAILJET_FINAL:
            continue
        try:
            req = urllib.request.Request(
                "https://api.mailjet.com/v3/REST/message/%s" % r['mailjet_message_id'],
                headers={"Authorization": f"Basic {auth}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            st = (data.get('Data') or [{}])[0].get('Status')
        except Exception as e:
            logger.info(f"license_alerts: statut Mailjet indisponible pour {r['id']} ({e})")
            continue
        if not st:
            continue
        # ⚠️ On horodate la vérification MÊME quand le statut n'a pas bougé.
        # Sinon l'UI ne peut pas distinguer « jamais vérifié » de « vérifié, et
        # toujours en attente chez Mailjet » — une valeur sans sa date
        # d'observation ment par omission (piège 17). `updated` ne compte, lui,
        # que les vrais changements d'état.
        changed = (st != r['mailjet_status'])
        try:
            with _connect() as conn:
                conn.execute(
                    "UPDATE alert_history SET mailjet_status=?, mailjet_checked_at=? WHERE id=?",
                    (st, datetime.now().isoformat(timespec='seconds'), r['id']))
                conn.commit()
            checked += 1
            if changed:
                updated += 1
        except Exception:
            pass
    return {"updated": updated, "checked": checked}


def get_status():
    """État de la tâche, tel que l'UI doit pouvoir l'affirmer sans deviner."""
    recipients = [r.strip() for r in os.environ.get('SAM_ALERT_TO', '').split(',') if r.strip()]
    hist = get_history(limit=1)
    now = datetime.now()
    nxt = now.replace(hour=ALERT_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return {
        "armed": bool(recipients and ALERT_FROM),
        "alert_hour": ALERT_HOUR,
        "recipients": recipients,
        "sender": ALERT_FROM,
        "mailjet_configured": bool(os.environ.get('MAILJET_API_KEY')
                                   and os.environ.get('MAILJET_SECRET_KEY')),
        "next_check": nxt.isoformat(timespec='seconds'),
        "already_checked_today": _already_sent_today(),
        "retention_days": HISTORY_RETENTION_DAYS,
        "last_run": hist[0] if hist else None,
    }


def _load_summary():
    """Délègue à la source unique — voir AgentMaster/license_view.py.

    ⚠️ Ne PAS réimplémenter ici : c'est la divergence entre cette lecture et
    celle de la page `/licenses` qui a produit « 2 périmés » à l'écran et
    « 29 » dans le mail le 01/09/2026.
    """
    try:
        from AgentMaster.license_view import load_summary
    except ImportError:
        from license_view import load_summary
    return load_summary()


def _in_scope(user):
    """Un profil relevant du perimetre Office 365 de l'organisation.

    L'agent remonte le `dn` AD depuis la 2.29.0 ; le perimetre est decide ici
    (cf. Web/core/o365_scope). Sur les 32 jetons perimes releves le 01/09, 9
    etaient hors perimetre : un compte local (487 j), un compte d'admin
    present sur 7 hotes, et un depart en OU exclue. Alerter dessus
    noie le signal — mais la PAGE, elle, sait toujours les montrer.

    ⚠️ Sans `dn` (agent < 2.29.0), on ne SAIT pas : on garde le profil plutot
    que de le taire. Ne jamais filtrer sur un critere qu'on n'a pas mesure.
    """
    # Comptes techniques : ecartables au NOM, sans DN (meme regle que
    # Web/core/o365_scope.is_technical — la page et le mail doivent voir le
    # meme perimetre, sinon on recree la divergence corrigee en 2.29.1).
    try:
        from Web.core.o365_scope import is_technical
    except ImportError:
        is_technical = lambda n: False
    if is_technical(user.get('name')):
        return False

    dn = user.get('dn')
    if not dn:
        return True
    up = dn.upper()
    if any(x.strip().upper() in up for x in SCOPE_EXCLUDE):
        return False
    return SCOPE_OU.upper() in up


def coverage(hosts):
    """(utilisateurs retenus, hôtes couverts) — la mesure de ce qui a pu être vu.

    Sert à qualifier un « rien à signaler » : sur une ferme dont personne n'a
    ouvert de session depuis l'horizon, l'absence d'alerte ne veut rien dire.
    """
    users = sum(len([u for u in (h.get('users') or []) if _in_scope(u)]) for h in hosts
                if (h.get('sca_status') or '') != 'not_installed')
    covered = sum(1 for h in hosts
                  if (h.get('sca_status') or '') != 'not_installed'
                  and any(_in_scope(u) for u in (h.get('users') or [])))
    return users, covered


def build_digest(hosts):
    """Construit (sujet, corps texte) ou None s'il n'y a rien à signaler.

    Signale : jetons périmés (>30 j — LE signal actionnable, réparable depuis le
    dashboard) et SCA désactivée (anomalie critique sur un hôte RDS). Les jetons
    « absents » ne déclenchent PAS d'alerte : la plupart sont des utilisateurs
    n'ayant jamais ouvert Office sur l'hôte, et la purge n'y peut rien.

    ⚠️ Chaque ligne porte SA date d'observation. Les profils étant en UPD, un
    utilisateur n'est mesurable que pendant sa session : le lot signalé mélange
    forcément des observations d'aujourd'hui et d'il y a plusieurs jours. Sans
    cette date, le lecteur croirait à une photo prise à l'instant.
    """
    stale, sca_off = [], []
    for h in hosts:
        if (h.get('sca_status') or '') == 'not_installed':
            continue
        if (h.get('sca_status') or '') == 'disabled':
            sca_off.append(h['hostname'])
        for u in (h.get('users') or []):
            if u.get('status') == 'stale' and _in_scope(u):
                stale.append((u.get('name', '?'), h['hostname'], u.get('age_days'),
                              u.get('observed_at'), bool(u.get('age_projected'))))

    seen_users, covered_hosts = coverage(hosts)
    cover_line = (f"Couverture du contrôle : {seen_users} utilisateur(s) observé(s) "
                  f"sur {covered_hosts} hôte(s) au cours des {OBSERVATION_HORIZON_DAYS} "
                  f"derniers jours.")

    # Perimetre INDETERMINE : un compte jamais revu depuis le passage en agent
    # 2.29.0 n'a pas encore de DN. On le GARDE (ne jamais filtrer sur un critere
    # qu'on n'a pas mesure) et on le DIT, sinon le bruit residuel se lit comme un
    # defaut du filtre. Se resorbe seul a mesure que les gens se connectent.
    indetermines = sorted({u.get('name') for h in hosts for u in (h.get('users') or [])
                           if u.get('status') == 'stale' and not u.get('dn')})

    if not stale and not sca_off:
        return None

    # 🔴 Regrouper PAR UTILISATEUR. Depuis 2.29.4 `load_summary` ne sert deja
    # plus qu'une observation par personne (profil UPD = un seul disque, la plus
    # recente supplante les autres hotes) ; ce regroupement reste utile pour les
    # profils persistants et les relevés anterieurs. Le geste de support est par personne : lister
    # le même compte d'administration sept fois n'ajoute aucune information et enterre les
    # autres cas. On garde le pire age et on cite les hotes.
    par_user = {}
    for name, host, age, observed, projected in stale:
        e = par_user.setdefault(name, {'hosts': [], 'age': 0, 'observed': observed, 'projected': projected})
        e['hosts'].append(host)
        if (age or 0) > e['age']:
            e['age'], e['observed'], e['projected'] = age, observed, projected
    stale = [(n, sorted(e['hosts']), e['age'], e['observed'], e['projected'])
             for n, e in par_user.items()]
    stale.sort(key=lambda x: -(x[2] or 0))
    lines = ["Contrôle quotidien des jetons Office 365 sur la ferme RDS.", ""]
    if sca_off:
        lines.append("SCA DÉSACTIVÉE (anomalie critique, à traiter en premier) :")
        lines += [f"  - {h}" for h in sorted(sca_off)]
        lines.append("")
    if stale:
        lines.append(f"Jetons périmés (> {STALE_DAYS} jours) — {len(stale)} utilisateur(s) :")
        for name, hosts_u, age, observed, projected in stale[:MAX_STALE_LISTED]:
            when = (observed or '')[:16].replace('T', ' ')
            marque = ' [estimé]' if projected else ''
            ou = ', '.join(hosts_u) if len(hosts_u) <= 3 else f"{len(hosts_u)} hôtes"
            lines.append(f"  - {name} sur {ou} ({age} j{marque}) — dernière mesure {when}")
        if len(stale) > MAX_STALE_LISTED:
            lines.append(f"  … et {len(stale) - MAX_STALE_LISTED} autre(s), "
                         f"liste complète sur le dashboard.")
        lines.append("")
        lines.append("Réparation : bouton « Réparer » dans le détail de l'hôte —")
        lines.append("session OUVERTE et Office FERMÉ (profils UPD)" + (f" — {DASHBOARD_URL}" if DASHBOARD_URL else "."))
        lines.append("")
    lines.append(cover_line)
    if indetermines:
        lines.append(f"⚠️ {len(indetermines)} compte(s) au périmètre indéterminé (unité d'organisation")
        lines.append("   pas encore remontée — ils n'ont pas rouvert de session depuis la mise à jour")
        lines.append(f"   des agents) : {', '.join(indetermines[:8])}"
                     + (" …" if len(indetermines) > 8 else ""))
        lines.append("   Ils sont conservés par prudence : on ne filtre pas sur un critère non mesuré.")
        lines.append("")
    perimetre = SCOPE_OU or "tous les comptes connus de l'annuaire"
    lines.append(f"Périmètre : {perimetre} (comptes d'administration, de service et clients exclus).")
    lines.append("Un utilisateur n'est mesurable que pendant sa session (profils UPD) : les dates")
    lines.append("de mesure s'étalent sur plusieurs jours, et « [estimé] » signale un âge PROJETÉ")
    lines.append("depuis la dernière mesure — c'est ce qui permet de voir venir un retour de congés.")

    n = len(stale)
    subject = (f"[SysAdminMaster] {n} jeton(s) Office périmé(s) sur la ferme RDS"
               if stale else "[SysAdminMaster] SCA désactivée sur la ferme RDS")
    return subject, "\n".join(lines)


def send_mail(subject, body, recipients):
    api_key = os.environ.get('MAILJET_API_KEY', '')
    secret = os.environ.get('MAILJET_SECRET_KEY', '')
    if not api_key or not secret:
        raise RuntimeError("Clés Mailjet absentes de l'environnement (MAILJET_API_KEY / MAILJET_SECRET_KEY)")

    payload = json.dumps({
        "Messages": [{
            "From": {"Email": ALERT_FROM, "Name": "SysAdminMaster"},
            "To": [{"Email": r.strip()} for r in recipients if r.strip()],
            "Subject": subject,
            "TextPart": body,
        }]
    }).encode('utf-8')

    auth = base64.b64encode(f"{api_key}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://api.mailjet.com/v3.1/send",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Mailjet HTTP {resp.status}")
        result = json.loads(resp.read().decode('utf-8'))
    msg = result.get('Messages', [{}])[0]
    status = msg.get('Status')
    if status != 'success':
        raise RuntimeError(f"Mailjet statut inattendu : {status}")
    # 🔴 `Status: success` signifie « accepté par l'API », PAS « délivré ».
    # On renvoie l'identifiant du message pour pouvoir interroger son sort réel
    # plus tard (cf. refresh_delivery) — c'est la seule preuve de livraison.
    to = (msg.get('To') or [{}])[0]
    return str(to.get('MessageID') or '') or None


def _already_sent_today():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f).get('last_sent') == datetime.now().strftime('%Y-%m-%d')
    except (OSError, ValueError):
        return False


def _mark_sent_today():
    try:
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'last_sent': datetime.now().strftime('%Y-%m-%d')}, f)
    except OSError as e:
        logger.warning(f"license_alerts: état non persisté ({e})")


def _counts(hosts):
    """(jetons périmés, hôtes SCA désactivée) — mêmes règles que build_digest."""
    stale = sca_off = 0
    for h in hosts:
        if (h.get('sca_status') or '') == 'not_installed':
            continue
        if (h.get('sca_status') or '') == 'disabled':
            sca_off += 1
        stale += sum(1 for u in (h.get('users') or [])
                     if u.get('status') == 'stale' and _in_scope(u))
    return stale, sca_off


def check_and_alert(force=False, dry_run=False, origin='daily'):
    """Un contrôle complet. Retourne True si un mail est parti (ou aurait dû en dry-run).

    ⚠️ CHAQUE issue est tracée dans `alert_history`, y compris « rien à signaler »
    et « alerte désactivée ». Sans ça, l'absence de mail est ambiguë : le 28/08/2026
    il a fallu interroger l'API Mailjet pour établir que la tâche fonctionnait et
    qu'il n'y avait simplement rien à envoyer. Le silence doit être lisible.
    """
    recipients = [r.strip() for r in os.environ.get('SAM_ALERT_TO', '').split(',') if r.strip()]
    if not recipients or not ALERT_FROM:
        manque = "SAM_ALERT_TO" if not recipients else "SAM_ALERT_FROM"
        logger.info(f"license_alerts: {manque} vide — alerte désactivée")
        record_run(origin, 0, 0, False, reason=f"{manque} vide — alerte désactivée")
        return False

    hosts = _load_summary()
    stale_count, sca_off_count = _counts(hosts)
    digest = build_digest(hosts)

    if digest is None:
        if not force:
            logger.info("license_alerts: aucun jeton périmé, pas d'envoi")
            seen_users, covered_hosts = coverage(hosts)
            record_run(origin, stale_count, sca_off_count, False,
                       reason=(f"Rien à signaler : aucun jeton périmé, aucune SCA "
                               f"désactivée — sur {seen_users} utilisateur(s) observé(s) "
                               f"sur {covered_hosts} hôte(s) depuis "
                               f"{OBSERVATION_HORIZON_DAYS} j"),
                       recipients=recipients)
            return False
        digest = ("[SysAdminMaster] Contrôle jetons Office — RAS (envoi forcé)",
                  "Contrôle quotidien des jetons Office 365 : aucun jeton périmé, "
                  "aucune SCA désactivée sur la ferme.\n\nEnvoi forcé de test.")

    subject, body = digest
    if dry_run:
        print(f"--- DRY RUN — destinataires : {recipients}\nSujet : {subject}\n\n{body}")
        return True

    try:
        message_id = send_mail(subject, body, recipients)
    except Exception as e:
        logger.error(f"license_alerts: envoi échoué — {e}")
        record_run(origin, stale_count, sca_off_count, False,
                   reason="Échec de l'envoi", recipients=recipients,
                   subject=subject, body=body, error=str(e))
        raise

    logger.info(f"license_alerts: mail envoyé à {recipients} — {subject}")
    record_run(origin, stale_count, sca_off_count, True, recipients=recipients,
               subject=subject, body=body, mailjet_message_id=message_id)
    return True


def daily_loop():
    """Boucle du thread démon lancé par master.py. Ne lève JAMAIS."""
    logger.info(f"license_alerts: contrôle quotidien armé ({ALERT_HOUR}h00, "
                f"destinataires configurés : {bool(os.environ.get('SAM_ALERT_TO'))}, expéditeur configuré : {bool(ALERT_FROM)})")
    while True:
        try:
            now = datetime.now()
            if now.hour == ALERT_HOUR and not _already_sent_today():
                # Marque le passage quoi qu'il arrive (envoi, rien à signaler, ou
                # échec) : sans ça la base serait reconsultée chaque minute pendant
                # une heure. `check_and_alert` a déjà tracé l'issue dans l'historique.
                try:
                    check_and_alert()
                finally:
                    _mark_sent_today()
        except Exception as e:
            logger.error(f"license_alerts: erreur du contrôle quotidien (ignorée) : {e}")
        time.sleep(60)


def start_daily_thread():
    t = threading.Thread(target=daily_loop, name='license-alerts', daemon=True)
    t.start()
    return t


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    ok = check_and_alert(force='--force' in sys.argv, dry_run='--dry-run' in sys.argv)
    sys.exit(0 if ok else 1)
