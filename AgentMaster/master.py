"""
AgentMaster - service d'ingestion (port 5000), sous Linux (systemd) ou Windows
Recoit les heartbeats des agents PowerShell et expose une API pour le Dashboard.

Usage:
    python master.py
    # ou
    .venv\\Scripts\\python.exe AgentMaster\\master.py

Tokens:
    X-Agent-Token  : CHANGE_ME__token_via_config_ou_env       (agents PowerShell -> master)
    X-Master-Token : (env SAM_MASTER_TOKEN)       (dashboard -> master)
    X-Read-Token   : (env SAM_READ_TOKEN)         (lecture seule : outils de supervision — 2.32.0)
"""

import os
import json
import sqlite3
import threading
import time
import random
import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import re
import hmac
from flask import Flask, request, jsonify, send_from_directory, abort

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
CONFIG_PATH = os.path.join(BASE_DIR, 'Config', 'config.json')
SCRIPTS_DIR = os.path.join(BASE_DIR, 'Scripts')

# Scripts servis aux agents pour l'auto-mise à jour (modèle pull). Liste blanche
# stricte : un agent ne peut télécharger que ces fichiers, jamais un chemin arbitraire.
PULLABLE_SCRIPTS = {'SysAdminMaster-Agent.ps1', 'Install-SysAdminMaster-Agent.ps1', 'Deploy-AgentRelay.ps1'}
_SAFE_SCRIPT_RE = re.compile(r'^[A-Za-z0-9._-]+\.ps1$')

def _load_config():
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

FULL_CONFIG = _load_config()

# --- Tokens (rotation v2.15.0) ---
# SAM_AGENT_TOKEN        : jeton courant (obligatoire).
# SAM_AGENT_TOKEN_LEGACY : ancien jeton, accepté UNIQUEMENT pendant une fenêtre de
#                          rotation (retirer la variable une fois la flotte migrée).
# ⛔ Plus AUCUN défaut en dur : le jeton historique a vécu dans le code, la doc et
# l'historique git pendant 6 mois — un fallback silencieux le ferait revivre à la
# première variable d'env oubliée. Fail-closed : sans jeton configuré, on refuse
# de démarrer plutôt que de démarrer ouvert.
AGENT_TOKEN = os.environ.get('SAM_AGENT_TOKEN') or FULL_CONFIG.get('Security', {}).get('AgentCommunicationToken')
AGENT_TOKEN_LEGACY = os.environ.get('SAM_AGENT_TOKEN_LEGACY') or None
if not AGENT_TOKEN:
    raise SystemExit("SAM_AGENT_TOKEN absent (env ou Config/config.json) — refus de démarrer sans jeton agent.")
MASTER_TOKEN = os.environ.get('SAM_MASTER_TOKEN') or FULL_CONFIG.get('AgentMaster', {}).get('Token')
if not MASTER_TOKEN:
    raise SystemExit("SAM_MASTER_TOKEN absent (env ou Config/config.json) — refus de démarrer sans jeton master.")

# Jeton de LECTURE SEULE (2.32.0, 02/11/2026) : consulte les routes GET de consultation (agents, historique,
# licences, alertes). Il ne peut JAMAIS mettre une commande en file : le jeton master commande la ferme
# (logoff, run_ps_command...), celui-ci est donné aux outils de supervision (tableaux de bord externes). Absent = aucun accès lecture.
READ_TOKEN = os.environ.get('SAM_READ_TOKEN') or None
if READ_TOKEN and READ_TOKEN == MASTER_TOKEN:
    raise SystemExit("SAM_READ_TOKEN identique au jeton master — refus : la lecture seule doit avoir son propre jeton.")

# Suivi de rotation : hôtes encore vus avec le jeton legacy (hostname -> iso datetime).
# Exposé dans /api/health pour piloter la fin de la fenêtre de transition.
LEGACY_TOKEN_HOSTS = {}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log_dir = os.path.join(BASE_DIR, 'Logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'agentmaster.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('AgentMaster')

# ---------------------------------------------------------------------------
# DatabaseManager (copie autonome depuis engine.py)
# ---------------------------------------------------------------------------

class DatabaseManager:
    def __init__(self, db_path='agentmaster.db'):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS agent_metrics (
                    hostname TEXT PRIMARY KEY,
                    data TEXT,
                    received_at TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS agent_commands (
                    id TEXT PRIMARY KEY,
                    hostname TEXT,
                    action TEXT,
                    params TEXT,
                    status TEXT,
                    created_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    result_message TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS license_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hostname TEXT NOT NULL,
                    recorded_at TIMESTAMP NOT NULL,
                    sca_status TEXT,
                    office_version TEXT,
                    users_ok INTEGER DEFAULT 0,
                    users_stale INTEGER DEFAULT 0,
                    users_missing INTEGER DEFAULT 0,
                    users_data TEXT
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_license_hostname_date
                ON license_history(hostname, recorded_at)
            ''')
            # Série temporelle CPU/RAM (v2.21.0).
            # `agent_metrics` fait INSERT OR REPLACE sur la PK hostname : il ne
            # contient QUE le dernier état, aucun historique. Les graphes de /agents
            # étaient donc alimentés uniquement par le JS de la page ouverte, et
            # pré-remplis de 20 zéros — ils affichaient « 0 % sur les 200 dernières
            # secondes » alors qu'on n'avait pas de données, et repartaient de zéro
            # à chaque rechargement (constaté 31/07/2026).
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS metrics_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hostname TEXT NOT NULL,
                    recorded_at TIMESTAMP NOT NULL,
                    cpu REAL,
                    ram REAL,
                    sessions INTEGER
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_metrics_hostname_date
                ON metrics_history(hostname, recorded_at)
            ''')
            # Historique des contrôles d'alerte (v2.27.0). Le schéma est défini
            # UNE SEULE FOIS, dans license_alerts (propriétaire de la fonction) :
            # ce module écrit les lignes, master.py les sert. Redéclarer les
            # colonnes ici recréerait la dette `save_license_snapshot`, dupliqué
            # en deux exemplaires qui écrivent dans deux bases différentes.
            try:
                from AgentMaster.license_alerts import ALERT_SCHEMA
            except ImportError:
                from license_alerts import ALERT_SCHEMA
            for stmt in ALERT_SCHEMA:
                cursor.execute(stmt)
            conn.commit()

    # Historique : 1 point par hôte et par minute (pas 1 par heartbeat).
    # Les agents battent toutes les ~10 s : tout écrire ferait 86 400 lignes/jour
    # pour 10 hôtes. C'est exactement l'erreur qui avait fait exploser
    # `license_history` à 276 000 lignes. Échantillonnage + purge dès l'écriture.
    HISTORY_SAMPLE_SECONDS = 60
    HISTORY_RETENTION_HOURS = 24

    def __init_history_state(self):
        if not hasattr(self, '_last_history_write'):
            self._last_history_write = {}

    def save_metrics(self, hostname, data):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            now = datetime.now()
            cursor.execute('''
                INSERT OR REPLACE INTO agent_metrics (hostname, data, received_at)
                VALUES (?, ?, ?)
            ''', (hostname, json.dumps(data), now.isoformat()))

            # --- série temporelle échantillonnée ---
            self.__init_history_state()
            last = self._last_history_write.get(hostname)
            if last is None or (now - last).total_seconds() >= self.HISTORY_SAMPLE_SECONDS:
                try:
                    ram = data.get('ram') or {}
                    sess = data.get('sessions') or {}
                    cursor.execute('''
                        INSERT INTO metrics_history (hostname, recorded_at, cpu, ram, sessions)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (hostname, now.isoformat(),
                          float(data.get('cpu') or 0),
                          float(ram.get('UsedPercent') or 0),
                          int(sess.get('Active') or 0)))
                    self._last_history_write[hostname] = now
                    # Purge glissante, au fil de l'eau (pas de tâche séparée à oublier)
                    cutoff = (now - timedelta(hours=self.HISTORY_RETENTION_HOURS)).isoformat()
                    cursor.execute('DELETE FROM metrics_history WHERE recorded_at < ?', (cutoff,))
                except (TypeError, ValueError) as e:
                    logger.warning(f"metrics_history {hostname}: point ignoré ({e})")
            conn.commit()

    def get_metrics_history(self, minutes=120):
        """Série temporelle CPU/RAM par hôte, pour les graphes de /agents."""
        since = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        out = {}
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute('''
                    SELECT hostname, recorded_at, cpu, ram, sessions
                    FROM metrics_history WHERE recorded_at >= ?
                    ORDER BY recorded_at
                ''', (since,)).fetchall()
            for host, ts, cpu, ram, sess in rows:
                out.setdefault(host, []).append(
                    {"t": ts, "cpu": cpu, "ram": ram, "sessions": sess})
        except Exception as e:
            logger.error(f"get_metrics_history: {e}")
        return out

    def get_all_metrics(self):
        metrics = {}
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT hostname, data FROM agent_metrics')
                for hostname, data_json in cursor.fetchall():
                    metrics[hostname] = json.loads(data_json)
        except Exception as e:
            logger.error(f"Erreur SQL get_all_metrics: {e}")
        return metrics

    def save_command(self, cmd):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO agent_commands
                (id, hostname, action, params, status, created_at, completed_at, result_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                cmd.get('id'), cmd.get('hostname'), cmd.get('action'),
                json.dumps(cmd.get('params', {})), cmd.get('status'),
                cmd.get('created_at'), cmd.get('completed_at'), cmd.get('result_message')
            ))
            conn.commit()

    # Peremption des commandes jamais servies. 48 h couvre deux boots du
    # broker (qui ne poll qu'une fois par jour) ; au-dela, executer une commande
    # d'il y a plusieurs jours est plus dangereux qu'utile.
    COMMAND_EXPIRY_HOURS = 48

    def get_pending_commands(self, hostname):
        # Une commande servie passe 'sent' DANS LA MEME transaction. Jusqu'au
        # 27/08/2026 le statut n'etait marque qu'en memoire : apres chaque
        # redemarrage du master, ce repli SQLite resservait les memes lignes a
        # CHAQUE heartbeat. Un restart_agent reste en file (2.24.0 : l'agent se
        # tue avant de POSTer son resultat) a ainsi retue l'agent du broker ~45 s apres
        # chacun de ses boots nocturnes, 27 nuits de suite (31/07 -> 27/08).
        commands = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                now = datetime.now()
                cutoff = (now - timedelta(hours=self.COMMAND_EXPIRY_HOURS)).isoformat()
                cursor.execute('''
                    UPDATE agent_commands
                    SET status = 'expired', completed_at = ?,
                        result_message = 'Expiree sans avoir ete servie (pending > 48 h)'
                    WHERE hostname = ? AND status = 'pending' AND created_at < ?
                ''', (now.isoformat(), hostname, cutoff))
                cursor.execute('''
                    SELECT id, action, params, status, created_at
                    FROM agent_commands
                    WHERE hostname = ? AND status = 'pending'
                ''', (hostname,))
                for row in cursor.fetchall():
                    commands.append({
                        "id": row[0], "action": row[1],
                        "params": json.loads(row[2]), "status": row[3],
                        "created_at": row[4], "hostname": hostname
                    })
                if commands:
                    cursor.executemany(
                        "UPDATE agent_commands SET status = 'sent' WHERE id = ?",
                        [(c['id'],) for c in commands])
                conn.commit()
        except Exception as e:
            logger.error(f"Erreur SQL get_pending_commands: {e}")
        return commands

    def mark_commands_sent(self, cmd_ids):
        """Persiste pending -> 'sent' pour les commandes servies depuis la file
        MEMOIRE. Sans cela, un redemarrage du master perd le marquage memoire et
        le repli SQLite les ressert (boucle restart_agent observee sur le broker)."""
        if not cmd_ids:
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "UPDATE agent_commands SET status = 'sent' WHERE id = ? AND status = 'pending'",
                    [(i,) for i in cmd_ids])
                conn.commit()
        except Exception as e:
            logger.error(f"Erreur SQL mark_commands_sent: {e}")

    # Une ligne est ecrite au maximum toutes les LICENSE_HEARTBEAT_HOURS meme si
    # rien n'a change : preuve de vivacite de la collecte, sans laquelle un agent
    # muet serait indiscernable d'un agent dont l'etat n'evolue pas.
    LICENSE_HEARTBEAT_HOURS = 24

    def save_license_snapshot(self, hostname, o365):
        """Persiste un snapshot Office 365 depuis le payload agent (v2.9.0).

        ECRITURE SUR CHANGEMENT UNIQUEMENT (correctif 2026-07-30).

        La version d'origine inserait une ligne a CHAQUE heartbeat (~10 s), alors
        que l'etat des licences ne bouge qu'exceptionnellement. Resultat mesure :
        285 905 lignes dont 276 084 pour le seul broker, base a 36 Mo, soit
        ~7 500 lignes/jour et par hote de pur bruit. Ce n'etait pas un historique,
        c'etait un journal de battement de coeur mal place.
        """
        if not o365:
            return

        snapshot = (
            o365.get('sca_status'),
            o365.get('office_version'),
            int(o365.get('users_ok', 0)),
            int(o365.get('users_stale', 0)),
            int(o365.get('users_missing', 0)),
            json.dumps(o365.get('users', []), sort_keys=True),
        )

        try:
            with sqlite3.connect(self.db_path) as conn:
                last = conn.execute('''
                    SELECT sca_status, office_version, users_ok, users_stale,
                           users_missing, users_data, recorded_at
                    FROM license_history
                    WHERE hostname = ?
                    ORDER BY recorded_at DESC LIMIT 1
                ''', (hostname,)).fetchone()

                if last is not None:
                    unchanged = tuple(last[:6]) == snapshot
                    if unchanged:
                        # Etat identique : on n'ecrit que si le dernier point date
                        # de plus de LICENSE_HEARTBEAT_HOURS.
                        try:
                            age = datetime.now() - datetime.fromisoformat(last[6])
                            if age.total_seconds() < self.LICENSE_HEARTBEAT_HOURS * 3600:
                                return
                        except (TypeError, ValueError):
                            # Horodatage illisible : on preferera reecrire une ligne
                            # propre plutot que de bloquer la collecte.
                            pass

                conn.execute('''
                    INSERT INTO license_history
                    (hostname, recorded_at, sca_status, office_version,
                     users_ok, users_stale, users_missing, users_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (hostname, datetime.now().isoformat()) + snapshot)
                conn.commit()
        except Exception as e:
            logger.error(f"Erreur save_license_snapshot {hostname}: {e}")

    def get_license_summary(self):
        """Dernier etat connu de CHAQUE utilisateur (source unique license_view).

        🔴 Renvoyait le dernier snapshot de chaque HOTE jusqu'au 01/09/2026. Sur
        une ferme en profils UPD, ce snapshot ne contient que les utilisateurs
        connectes a cet instant : la page annoncait 2 jetons perimes quand il y
        en avait 29. Le repli ci-dessous conserve l'ancienne lecture au cas ou le
        module partage serait indisponible — mais il DOIT rester un repli.
        """
        try:
            try:
                from AgentMaster.license_view import load_summary
            except ImportError:
                from license_view import load_summary
            return load_summary()
        except Exception as e:
            logger.warning(f"license_view indisponible, repli sur le dernier snapshot par hote : {e}")
            return self._get_license_summary_legacy()

    def _get_license_summary_legacy(self):
        """Repli : dernier snapshot connu par hote (lecture partielle, cf. ci-dessus)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute('''
                    SELECT h.hostname, h.recorded_at, h.sca_status, h.office_version,
                           h.users_ok, h.users_stale, h.users_missing, h.users_data
                    FROM license_history h
                    INNER JOIN (
                        SELECT hostname, MAX(recorded_at) AS max_ts
                        FROM license_history GROUP BY hostname
                    ) latest ON h.hostname=latest.hostname AND h.recorded_at=latest.max_ts
                    ORDER BY h.hostname
                ''').fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Erreur get_license_summary: {e}")
            return []

    def get_command(self, cmd_id):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT id, hostname, action, params, status, created_at, completed_at, result_message '
                    'FROM agent_commands WHERE id = ?', (cmd_id,)
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "id": row[0], "hostname": row[1], "action": row[2],
                        "params": json.loads(row[3]), "status": row[4],
                        "created_at": row[5], "completed_at": row[6],
                        "result_message": row[7]
                    }
        except Exception as e:
            logger.error(f"Erreur SQL get_command: {e}")
        return None


# ---------------------------------------------------------------------------
# AgentState (dict en memoire + RLock)
# ---------------------------------------------------------------------------

class AgentState:
    def __init__(self):
        self.lock = threading.RLock()
        self.metrics = {}      # {hostname: data_dict}
        self.commands = {}     # {hostname: [cmd, ...]}

    def update_metrics(self, hostname, data):
        with self.lock:
            self.metrics[hostname] = data

    def get_all_metrics(self):
        with self.lock:
            return dict(self.metrics)

    def add_command(self, hostname, command):
        with self.lock:
            if hostname not in self.commands:
                self.commands[hostname] = []
            if len(self.commands[hostname]) >= 100:
                self.commands[hostname].pop(0)
            self.commands[hostname].append(command)

    def pop_pending_commands(self, hostname):
        with self.lock:
            cmds = [c for c in self.commands.get(hostname, []) if c.get('status') == 'pending']
            # Marquer comme "sent" pour ne pas les renvoyer une deuxieme fois
            for c in cmds:
                c['status'] = 'sent'
            return cmds

    def update_command(self, hostname, cmd_id, status, result_message):
        with self.lock:
            for cmd in self.commands.get(hostname, []):
                if cmd.get('id') == cmd_id:
                    cmd['status'] = status
                    cmd['completed_at'] = datetime.now().isoformat()
                    cmd['result_message'] = result_message
                    return cmd
        return None


# ---------------------------------------------------------------------------
# Instances globales
# ---------------------------------------------------------------------------

db = DatabaseManager(os.path.join(BASE_DIR, 'agentmaster.db'))
state = AgentState()

# Charger les metriques persistees au demarrage
for hostname, data in db.get_all_metrics().items():
    state.update_metrics(hostname, data)

logger.info(f"AgentMaster demarre avec {len(state.metrics)} agents en memoire (depuis SQLite)")

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

app = Flask(__name__)


# --- Helpers auth ---

def _check_agent_token():
    """Valide le jeton agent. Pendant une rotation, le jeton legacy reste accepté
    et son usage est TRACÉ par hôte (LEGACY_TOKEN_HOSTS) : la fenêtre de transition
    ne se ferme que sur preuve que plus personne ne l'utilise, pas au doigt mouillé."""
    token = request.headers.get('X-Agent-Token', '')
    if token == AGENT_TOKEN:
        return True
    if AGENT_TOKEN_LEGACY and token == AGENT_TOKEN_LEGACY:
        try:
            payload = request.get_json(silent=True) or {}
            host = (payload.get('hostname') or request.remote_addr or '?').upper()
        except Exception:
            host = request.remote_addr or '?'
        first_seen = host not in LEGACY_TOKEN_HOSTS
        LEGACY_TOKEN_HOSTS[host] = datetime.now().isoformat()
        if first_seen:
            logger.warning(f"Jeton LEGACY utilise par {host} — hote a migrer (update_agent avec le nouveau token)")
        return True
    return False

def _check_master_token():
    token = request.headers.get('X-Master-Token', '')
    if token != MASTER_TOKEN:
        return False
    return True


def _check_read_token():
    """Consultation : le jeton master OU le jeton de lecture seule (X-Read-Token). Réservé aux routes GET
    qui ne modifient rien ; une route qui commande ou écrit garde _check_master_token."""
    if _check_master_token():
        return True
    token = request.headers.get('X-Read-Token', '')
    return bool(READ_TOKEN) and bool(token) and hmac.compare_digest(token, READ_TOKEN)


# ---------------------------------------------------------------------------
# Routes agent-facing  (X-Agent-Token)
# ---------------------------------------------------------------------------

@app.route('/api/agent/metrics', methods=['POST'])
def receive_agent_metrics():
    if not _check_agent_token():
        logger.warning(f"Acces refuse (token invalide) depuis {request.remote_addr}")
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or 'hostname' not in data:
        return jsonify({"error": "Invalid payload"}), 400

    hostname = data['hostname'].upper().strip().split('.')[0]
    data['received_at'] = datetime.now().isoformat()

    logger.info(f"Heartbeat recu: {hostname} (sessions={data.get('sessions', {}).get('Total', '?')})")

    # Sauvegarder en memoire + SQLite
    state.update_metrics(hostname, data)
    db.save_metrics(hostname, data)

    # Snapshot licences Office 365 si present dans le payload (v2.9.0)
    o365 = data.get('office_licenses')
    if o365:
        db.save_license_snapshot(hostname, o365)

    # Retourner les commandes pending (depuis memoire, fallback SQLite).
    # Dans les deux cas le passage a 'sent' est persiste en base : une commande
    # ne doit jamais pouvoir etre servie deux fois (REX du broker, 08/2026).
    pending = state.pop_pending_commands(hostname)
    if pending:
        db.mark_commands_sent([c.get('id') for c in pending])
    else:
        pending = db.get_pending_commands(hostname)

    return jsonify({"status": "ok", "commands": pending})


@app.route('/api/agent/command-result', methods=['POST'])
def receive_command_result():
    if not _check_agent_token():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    hostname = data.get('hostname', '').upper().strip().split('.')[0]
    cmd_id = data.get('commandId', '')
    status = data.get('status', 'completed')
    result_message = data.get('message', '')

    logger.info(f"Resultat commande: {hostname} / {cmd_id} -> {status}")

    # Mettre a jour en memoire
    cmd = state.update_command(hostname, cmd_id, status, result_message)

    # Persister en SQLite
    if cmd:
        db.save_command(cmd)
    else:
        # Commande pas en memoire, maj directe SQLite
        try:
            import sqlite3 as _sql
            with _sql.connect(db.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE agent_commands
                    SET status = ?, completed_at = ?, result_message = ?
                    WHERE id = ?
                ''', (status, datetime.now().isoformat(), result_message, cmd_id))
                conn.commit()
        except Exception as e:
            logger.error(f"Erreur maj commande SQLite: {e}")

    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Routes dashboard-facing  (X-Master-Token)
# ---------------------------------------------------------------------------

@app.route('/api/master/agents', methods=['GET'])
def get_all_agents():
    if not _check_read_token():
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify(state.get_all_metrics())


@app.route('/api/master/command', methods=['POST'])
def queue_command():
    if not _check_master_token():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    hostname = (data.get('hostname') or '').upper().strip().split('.')[0]
    action = data.get('action', '')
    params = data.get('params', {})

    if not hostname or not action:
        return jsonify({"error": "hostname et action requis"}), 400

    cmd_id = f"{action}_{int(time.time())}_{random.randint(1000, 9999)}"
    command = {
        "id": cmd_id,
        "hostname": hostname,
        "action": action,
        "params": params,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "result_message": None
    }

    state.add_command(hostname, command)
    db.save_command(command)

    logger.info(f"Commande en queue: {cmd_id} -> {hostname}/{action}")
    return jsonify({"status": "ok", "commandId": cmd_id})


@app.route('/api/master/command-status/<cmd_id>', methods=['GET'])
def get_command_status(cmd_id):
    if not _check_master_token():
        return jsonify({"error": "Unauthorized"}), 401

    cmd = db.get_command(cmd_id)
    if cmd:
        return jsonify(cmd)
    return jsonify({"error": "Commande inconnue"}), 404


# ---------------------------------------------------------------------------
# Agent deploy (X-Master-Token) — execute depuis le broker via WinRM (master sous Windows)
# ---------------------------------------------------------------------------

@app.route('/api/master/agent-deploy', methods=['POST'])
def master_agent_deploy():
    """
    Reçoit un ordre de deploiement depuis le Dashboard et l'execute localement
    (depuis le broker, qui a l'acces WinRM aux hotes RDS).
    Payload: {action, targets: ["RDSH01",...], script_content, agent_script_content}
    """
    if not _check_master_token():
        return jsonify({"error": "Unauthorized"}), 401

    # Garde de plateforme (migration Linux 2026-06-22) : le déploiement d'agents
    # s'appuie sur PowerShell/WinRM (Windows). Sous Linux, déployer les agents via
    # le modèle pull (Bootstrap-Agent-FromMaster.ps1 / GPO / script local sur chaque RDS).
    import platform as _platform
    if _platform.system() != 'Windows':
        logger.warning("agent-deploy appelé sous Linux : opération PowerShell/WinRM indisponible.")
        return jsonify({
            "success": False,
            "error": "Déploiement d'agents indisponible : l'AgentMaster tourne sous Linux (pas de PowerShell/WinRM). "
                     "Déployer/mettre à jour les agents via le modèle pull (Bootstrap-Agent-FromMaster.ps1), GPO, ou script local sur chaque hôte RDS."
        }), 501

    import subprocess
    import tempfile

    data = request.get_json() or {}
    action = data.get('action', 'diagnose').strip().lower()
    targets = data.get('targets', [])
    script_content = data.get('script_content', '')
    agent_content = data.get('agent_script_content', '')

    if not targets:
        return jsonify({"error": "targets requis"}), 400
    if not script_content:
        return jsonify({"error": "script_content requis"}), 400

    # Exclure le broker lui-meme (il a un role different)
    broker_short = os.environ.get('COMPUTERNAME', '').upper()
    targets = [h for h in targets if h.upper() != broker_short]
    if not targets:
        return jsonify({"success": False, "error": "Aucune cible valide (le broker est exclu)"}), 400

    # Ecrire le script d'install dans un fichier temporaire
    # Le script agent DOIT etre dans le MEME repertoire sous le nom SysAdminMaster-Agent.ps1
    # car Install-SysAdminMaster-Agent.ps1 utilise $PSScriptRoot pour le localiser
    script_path = None
    agent_path = None
    results = {}

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False,
                                         encoding='utf-8', prefix='install_agent_',
                                         dir=tempfile.gettempdir()) as f:
            f.write(script_content)
            script_path = f.name

        if agent_content:
            # Meme repertoire, nom exact attendu par $PSScriptRoot
            agent_path = os.path.join(os.path.dirname(script_path), 'SysAdminMaster-Agent.ps1')
            with open(agent_path, 'w', encoding='utf-8') as f:
                f.write(agent_content)

        for host in targets:
            host = host.upper().strip().split('.')[0]
            logger.info(f"agent-deploy: {action} sur {host}")

            if action == 'diagnose':
                ps_cmd = (
                    f"Invoke-Command -ComputerName {host} -ScriptBlock {{"
                    f"$t=Get-ScheduledTask -TaskName SysAdminMasterAgent -ErrorAction SilentlyContinue;"
                    f"if($t){{\"$($t.TaskName): $($t.State)\"}}else{{\"Tache introuvable\"}}"
                    f"}} -ErrorAction Stop"
                )
                try:
                    proc = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", ps_cmd],
                        capture_output=True, text=True, timeout=30,
                        encoding='utf-8', errors='replace'
                    )
                    out = proc.stdout.strip() or proc.stderr.strip()
                    results[host] = {'success': proc.returncode == 0, 'output': out}
                except Exception as e:
                    results[host] = {'success': False, 'output': str(e)}

            elif action == 'restart':
                ps_cmd = (
                    f"Invoke-Command -ComputerName {host} -ScriptBlock {{"
                    f"Stop-ScheduledTask SysAdminMasterAgent -ErrorAction SilentlyContinue;"
                    f"Start-Sleep 1; Start-ScheduledTask SysAdminMasterAgent; 'Agent redemarre'"
                    f"}} -ErrorAction Stop"
                )
                try:
                    proc = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", ps_cmd],
                        capture_output=True, text=True, timeout=30,
                        encoding='utf-8', errors='replace'
                    )
                    out = proc.stdout.strip() or proc.stderr.strip()
                    results[host] = {'success': proc.returncode == 0, 'output': out}
                except Exception as e:
                    results[host] = {'success': False, 'output': str(e)}

            else:
                # install / uninstall : utiliser le script fourni
                ps_args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-File", script_path, "-ComputerName", host]
                if action == 'uninstall':
                    ps_args += ["-Uninstall"]
                try:
                    proc = subprocess.run(
                        ps_args, capture_output=True, text=True,
                        timeout=180, encoding='utf-8', errors='replace'
                    )
                    out = proc.stdout + proc.stderr
                    results[host] = {'success': proc.returncode == 0, 'output': out}
                except subprocess.TimeoutExpired:
                    results[host] = {'success': False, 'output': 'Timeout (180s)'}
                except Exception as e:
                    results[host] = {'success': False, 'output': str(e)}

    finally:
        for p in [script_path, agent_path]:
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass

    success = all(r['success'] for r in results.values())
    logger.info(f"agent-deploy {action}: {len(results)} hosts, success={success}")
    return jsonify({"success": success, "results": results})


# ---------------------------------------------------------------------------
# Service de scripts agent (X-Agent-Token) — modèle pull / auto-update
# ---------------------------------------------------------------------------

@app.route('/scripts/<path:name>', methods=['GET'])
def serve_agent_script(name):
    """
    Sert un script agent pour l'auto-mise à jour (modèle pull).
    L'agent télécharge ce fichier via Invoke-WebRequest (header X-Agent-Token)
    puis se réinstalle. 100 % HTTP → pilotable depuis Linux, sans WinRM.
    Liste blanche stricte + anti-traversée de répertoire.
    """
    if not _check_agent_token():
        logger.warning(f"/scripts refusé (token invalide) depuis {request.remote_addr}")
        return jsonify({"error": "Unauthorized"}), 401

    base = os.path.basename(name)
    if base != name or not _SAFE_SCRIPT_RE.match(base) or base not in PULLABLE_SCRIPTS:
        logger.warning(f"/scripts refusé (nom non autorisé): '{name}' depuis {request.remote_addr}")
        abort(404)

    if not os.path.exists(os.path.join(SCRIPTS_DIR, base)):
        return jsonify({"error": f"Script introuvable : {base}"}), 404

    logger.info(f"/scripts servi: {base} -> {request.remote_addr}")
    return send_from_directory(SCRIPTS_DIR, base, mimetype='text/plain; charset=utf-8')


# ---------------------------------------------------------------------------
# Licences Office 365 (X-Master-Token) — v2.9.0
# ---------------------------------------------------------------------------

@app.route('/api/master/history', methods=['GET'])
def get_history():
    """Série temporelle CPU/RAM par hôte (défaut : 2 h). Alimente les graphes."""
    if not _check_read_token():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        minutes = max(5, min(int(request.args.get('minutes', 120)), 1440))
    except (TypeError, ValueError):
        minutes = 120
    return jsonify({"status": "ok", "minutes": minutes,
                    "hosts": db.get_metrics_history(minutes)})


def _alerts():
    """Module license_alerts, quel que soit le mode de lancement du master."""
    try:
        from AgentMaster import license_alerts
    except ImportError:
        import license_alerts
    return license_alerts


@app.route('/api/master/alerts/status', methods=['GET'])
def master_alert_status():
    """État de la tâche d'alerte + dernier contrôle."""
    if not _check_read_token():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"status": "ok", "alerts": _alerts().get_status()})


@app.route('/api/master/alerts/history', methods=['GET'])
def master_alert_history():
    """Historique des contrôles — y compris ceux qui n'ont RIEN envoyé."""
    if not _check_read_token():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        limit = min(int(request.args.get('limit', 60)), 500)
    except ValueError:
        limit = 60
    return jsonify({"status": "ok", "runs": _alerts().get_history(limit)})


@app.route('/api/master/alerts/refresh-delivery', methods=['POST'])
def master_alert_refresh_delivery():
    """Réinterroge Mailjet sur le sort réel des messages (lecture externe seule)."""
    if not _check_master_token():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        res = _alerts().refresh_delivery()
        return jsonify({"status": "ok", **res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/master/alerts/test', methods=['POST'])
def master_alert_test():
    """⚠️ ENVOIE UN VRAI MAIL aux destinataires configurés (contrôle forcé).

    Bloqué sans condition par le garde-fou e2e (`ALWAYS_BLOCK`) : un POST /api/*
    non catalogué passe par défaut dans prod-safe, et la QA clique tous les
    boutons — sans cette entrée, chaque run enverrait un mail réel.
    """
    if not _check_master_token():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        sent = _alerts().check_and_alert(force=True, origin='manual')
        return jsonify({"status": "ok", "sent": bool(sent)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/master/licenses', methods=['GET'])
def get_licenses():
    if not _check_read_token():
        return jsonify({"error": "Unauthorized"}), 401
    summary = db.get_license_summary()
    # 🔴 Ne desserialiser QUE si `users_data` est present (chemin de repli).
    # La source unique `license_view` renvoie deja `users` sous forme de liste :
    # un `row.pop('users_data', '[]')` inconditionnel ECRASAIT cette liste par []
    # et la page affichait 0 partout — un vide qui se lit « rien a signaler ».
    for row in summary:
        if 'users_data' in row:
            try:
                row['users'] = json.loads(row.pop('users_data') or '[]')
            except Exception:
                row['users'] = []
        row.setdefault('users', [])
    return jsonify({"status": "ok", "hosts": summary})


# ---------------------------------------------------------------------------
# Health check (sans auth, pour monitoring)
# ---------------------------------------------------------------------------

@app.route('/api/health', methods=['GET'])
def health():
    metrics = state.get_all_metrics()
    now = datetime.now()
    online = 0
    for data in metrics.values():
        try:
            received = datetime.fromisoformat(data.get('received_at', ''))
            if (now - received).total_seconds() < 120:
                online += 1
        except (ValueError, TypeError):
            pass

    out = {
        "service": "AgentMaster",
        "status": "ok",
        "agents_total": len(metrics),
        "agents_online": online,
        "timestamp": now.isoformat()
    }
    # Pendant une rotation de jeton : visibilité sur qui traîne encore en legacy.
    # Un hôte migré reste dans LEGACY_TOKEN_HOSTS avec son dernier horodatage —
    # « still_on_legacy » ne compte que l'usage des 5 dernières minutes (un agent
    # heartbeate toutes les ~10 s : 5 min sans legacy = il est passé au nouveau).
    # Ne rien exposer hors fenêtre de rotation.
    if AGENT_TOKEN_LEGACY:
        recent = {}
        for h, ts in LEGACY_TOKEN_HOSTS.items():
            try:
                if (now - datetime.fromisoformat(ts)).total_seconds() < 300:
                    recent[h] = ts
            except (ValueError, TypeError):
                recent[h] = ts
        out["token_rotation"] = {
            "legacy_accepted": True,
            "hosts_still_on_legacy": sorted(recent.keys()),
            "last_seen": LEGACY_TOKEN_HOSTS,
        }
    return jsonify(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # Alerte quotidienne jetons Office (v2.14.0) — thread demon isole, une erreur
    # d'alerte ne peut pas toucher l'ingestion. Sans SAM_ALERT_TO : inactif.
    try:
        from AgentMaster.license_alerts import start_daily_thread
    except ImportError:
        from license_alerts import start_daily_thread  # lancement direct depuis AgentMaster/
    start_daily_thread()

    logger.info(f"AgentMaster v1.0 - Demarrage sur port {os.environ.get('SAM_MASTER_PORT', '5000')}")
    app.run(host='0.0.0.0', port=int(os.environ.get('SAM_MASTER_PORT', '5000')), debug=False)
