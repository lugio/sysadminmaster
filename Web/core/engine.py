import os
import json
import sqlite3
import threading
import time
import psutil
import logging
import subprocess
import random
from datetime import datetime
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

# Logging configuration
logger = logging.getLogger(__name__)

# Paths
BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPTS_DIR = os.path.join(BASE_DIR, 'Scripts')
CONFIG_PATH = os.path.join(BASE_DIR, 'Config', 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'Logs')

class SmartCache:
    def __init__(self, max_size=1000, default_ttl=30):
        self.cache = {}
        self.access_times = {}
        self.max_size = max_size
        self.default_ttl = default_ttl
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.cache:
                if time.time() - self.access_times[key]['timestamp'] > self.access_times[key]['ttl']:
                    del self.cache[key]
                    del self.access_times[key]
                    return None
                self.access_times[key]['timestamp'] = time.time()
                return self.cache[key]
            return None

    def set(self, key, value, ttl=None):
        with self.lock:
            if len(self.cache) >= self.max_size:
                current_time = time.time()
                expired_keys = [k for k, v in self.access_times.items()
                              if current_time - v['timestamp'] > v['ttl']]
                for k in expired_keys:
                    del self.cache[k]
                    del self.access_times[k]

                if len(self.cache) >= self.max_size:
                    oldest_key = min(self.access_times.keys(),
                                   key=lambda k: self.access_times[k]['timestamp'])
                    del self.cache[oldest_key]
                    del self.access_times[oldest_key]

            self.cache[key] = value
            self.access_times[key] = {
                'timestamp': time.time(),
                'ttl': ttl or self.default_ttl
            }

    def clear(self):
        with self.lock:
            self.cache.clear()
            self.access_times.clear()

class DatabaseManager:
    def __init__(self, db_path='sysadminmaster.db'):
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
            conn.commit()

    def save_metrics(self, hostname, data):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO agent_metrics (hostname, data, received_at)
                VALUES (?, ?, ?)
            ''', (hostname, json.dumps(data), datetime.now().isoformat()))
            conn.commit()

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

    LICENSE_HEARTBEAT_HOURS = 24

    def save_license_snapshot(self, hostname, o365):
        """Persiste un snapshot Office 365 depuis le payload agent (v2.9.0).

        ECRITURE SUR CHANGEMENT UNIQUEMENT (correctif 2026-07-30) — meme logique
        que AgentMaster/master.py.save_license_snapshot(), dont ce code est un
        DUPLICATA exact (deux ecrivains, deux bases : agentmaster.db pour le
        master, sysadminmaster.db ici). Ce chemin-ci parait mort — le dashboard
        lit le master via master_client — mais il n'est pas supprime tant que la
        cause racine de la duplication n'est pas etablie. Toute correction doit
        donc etre appliquee AUX DEUX endroits.
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

                if last is not None and tuple(last[:6]) == snapshot:
                    try:
                        age = datetime.now() - datetime.fromisoformat(last[6])
                        if age.total_seconds() < self.LICENSE_HEARTBEAT_HOURS * 3600:
                            return
                    except (TypeError, ValueError):
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
        """Dernier snapshot connu par hote + total ferme."""
        rows = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute('''
                    SELECT h.hostname, h.recorded_at, h.sca_status, h.office_version,
                           h.users_ok, h.users_stale, h.users_missing, h.users_data
                    FROM license_history h
                    INNER JOIN (
                        SELECT hostname, MAX(recorded_at) AS max_ts
                        FROM license_history
                        GROUP BY hostname
                    ) latest ON h.hostname = latest.hostname AND h.recorded_at = latest.max_ts
                    ORDER BY h.hostname
                ''').fetchall()
        except Exception as e:
            logger.error(f"Erreur get_license_summary: {e}")
            return []
        return [dict(r) for r in rows]

    def get_license_history(self, hostname, limit=48):
        """Historique des snapshots d'un hote (pour graphe tendance)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute('''
                    SELECT recorded_at, sca_status, users_ok, users_stale, users_missing
                    FROM license_history WHERE hostname = ?
                    ORDER BY recorded_at DESC LIMIT ?
                ''', (hostname, limit)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Erreur get_license_history {hostname}: {e}")
            return []

    def get_pending_commands(self, hostname):
        commands = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, action, params, status, created_at 
                    FROM agent_commands 
                    WHERE hostname = ? AND status = 'pending'
                ''', (hostname,))
                for row in cursor.fetchall():
                    commands.append({
                        "id": row[0], "action": row[1], 
                        "params": json.loads(row[2]), "status": row[3], 
                        "created_at": row[4],
                        "hostname": hostname
                    })
        except Exception as e:
            logger.error(f"Erreur SQL get_pending_commands: {e}")
        return commands

# Global Instances
cache = SmartCache()
db = DatabaseManager(os.path.join(BASE_DIR, 'sysadminmaster.db'))

def load_full_config():
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Erreur chargement config complete: {e}")
    return {}

FULL_CONFIG = load_full_config()

# Override des secrets depuis l'environnement si présents (audit sécu 2026-06-22) :
# permet de sortir les tokens de config.json. En prod, définir SAM_AGENT_TOKEN / SAM_MASTER_TOKEN.
_env_agent_token = os.environ.get('SAM_AGENT_TOKEN')
if _env_agent_token:
    FULL_CONFIG.setdefault('Security', {})['AgentCommunicationToken'] = _env_agent_token
_env_master_token = os.environ.get('SAM_MASTER_TOKEN')
if _env_master_token:
    FULL_CONFIG.setdefault('AgentMaster', {})['Token'] = _env_master_token

RDS_CONFIG = FULL_CONFIG.get('RDSServers', {})

class GlobalState:
    def __init__(self):
        self.lock = threading.RLock()
        self.stats = {
            "metrics": {"CPU": 0, "Memory": {"UsedPercent": 0}, "Disks": []},
            "sessions": [],
            "profiles": [],
            "servers": RDS_CONFIG.get('SessionHosts', ["Local"]),
            "last_update": 0,
            "agent_metrics": db.get_all_metrics(),
            "agent_commands": {},
            "health_status": "unknown",
            "performance_history": deque(maxlen=100)
        }
        self.web_metrics = {
            'requests_total': 0,
            'requests_by_endpoint': defaultdict(int),
            'response_times': deque(maxlen=1000),
            'errors_total': 0,
            'active_connections': 0,
            'start_time': time.time(),
            'memory_usage': [],
            'cpu_usage': []
        }

    def get_stats(self):
        with self.lock:
            return self.stats.copy()

    def update_stats(self, key, value):
        with self.lock:
            self.stats[key] = value

    def get_web_metrics(self):
        with self.lock:
            return self.web_metrics.copy()

    def update_web_metrics(self, key, value):
        with self.lock:
            self.web_metrics[key] = value

# Global Instance
global_state = GlobalState()

def analyze_health(metrics, sessions, servers, agent_metrics):
    score = 100
    if metrics.get("CPU", 0) > 90: score -= 30
    elif metrics.get("CPU", 0) > 70: score -= 15
    if metrics.get("Memory", {}).get("UsedPercent", 0) > 90: score -= 30
    elif metrics.get("Memory", {}).get("UsedPercent", 0) > 80: score -= 15
    if len(sessions) > 100: score -= 10
    for disk in metrics.get("Disks", []):
        if disk.get("UsedPercent", 0) > 90: score -= 20
    if len(servers) == 0: score -= 50
    for hostname, data in agent_metrics.items():
        for svc in data.get('services', []):
            if svc.get('Status') != 'Running': score -= 10
    if score >= 80: return "healthy"
    elif score >= 60: return "warning"
    else: return "critical"

def get_rd_servers():
    # Zero-WinRM: Return configured servers only, no remote calls
    return RDS_CONFIG.get('SessionHosts', ["Local"])

def get_all_rds_sessions():
    """Agrege les sessions de tous les agents (Zero-WinRM)."""
    stats = global_state.get_stats()
    agent_metrics = stats.get("agent_metrics", {})
    all_sessions = []
    
    for host, data in agent_metrics.items():
        sess_summary = data.get('sessions', {})
        
        # DEBUG LOG
        # logger.info(f"Processing sessions for {host}: List keys: {list(sess_summary.keys()) if isinstance(sess_summary, dict) else 'NOT_A_DICT'}")

        # Cas 1: Agent v2.7.6+ (Liste detaillee avec noms d'utilisateurs)
        if isinstance(sess_summary, dict) and 'List' in sess_summary and isinstance(sess_summary['List'], list) and len(sess_summary['List']) > 0:
            # Jointure charge par utilisateur (agents v2.25.0+) : cle = username
            # en minuscules. Agent plus ancien -> pas de user_load -> les champs
            # restent absents et l'UI affiche honnetement "non disponible".
            load_by_user = {}
            for ul in (data.get('user_load') or []):
                if isinstance(ul, dict) and ul.get('user'):
                    load_by_user[str(ul['user']).lower()] = ul
            for s in sess_summary['List']:
                if isinstance(s, dict):
                    if 'HostServer' not in s: s['HostServer'] = host
                    sc = s.copy()
                    ul = load_by_user.get(str(sc.get('UserName', '')).lower())
                    if ul:
                        sc['CpuPct'] = ul.get('cpu')
                        sc['MemMB'] = ul.get('mem_mb')
                        sc['Procs'] = ul.get('procs')
                        sc['TopProcs'] = ul.get('top') or []
                    all_sessions.append(sc)
            continue

        # Cas 2: Fallback Agents anciens (Compteurs seulement)
        active = sess_summary.get('Active', 0)
        disconnected = sess_summary.get('Disconnected', 0)
        
        for i in range(active):
            all_sessions.append({
                "SessionId": f"{host}-A-{i}",
                "UserName": f"Utilisateur {i+1}",
                "HostServer": host,
                "SessionState": "Active",
                "IdleTime": "0m"
            })
        for i in range(disconnected):
            all_sessions.append({
                "SessionId": f"{host}-D-{i}",
                "UserName": f"Utilisateur {i+1}",
                "HostServer": host,
                "SessionState": "Disconnected",
                "IdleTime": "N/A"
            })
            
    return all_sessions

def fetch_data_loop():
    global global_state
    while True:
        try:
            # Import local pour eviter import circulaire
            from Web.core.master_client import get_agents as _mc_get_agents

            # Puller les metriques agents depuis l'AgentMaster
            agent_metrics = _mc_get_agents()
            if agent_metrics:
                global_state.update_stats("agent_metrics", agent_metrics)
                logger.debug(f"fetch_data_loop: {len(agent_metrics)} agents recus depuis AgentMaster")
            else:
                # Garder les donnees stale si le master est injoignable
                agent_metrics = global_state.get_stats().get("agent_metrics", {})

            # Metriques locales du Dashboard (psutil)
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disks = []
            for part in psutil.disk_partitions():
                if 'fixed' in part.opts or part.fstype:
                    try:
                        usage = psutil.disk_usage(part.mountpoint)
                        disks.append({
                            "Drive": part.mountpoint, "UsedPercent": usage.percent,
                            "TotalGB": round(usage.total / (1024**3), 1),
                            "UsedGB": round(usage.used / (1024**3), 1),
                            "FreeGB": round(usage.free / (1024**3), 1)
                        })
                    except: continue

            metrics = {
                "CPU": cpu, "Memory": {"UsedPercent": mem.percent, "TotalGB": round(mem.total / (1024**3), 1), "UsedGB": round(mem.used / (1024**3), 1), "FreeGB": round(mem.available / (1024**3), 1)},
                "Disks": disks, "Uptime": "N/A", "Timestamp": datetime.now().isoformat()
            }
            # Persister les metriques locales (host du Dashboard) : sinon la vue "Broker Local"
            # restait bloquee sur les zeros d'initialisation (portage Linux 2026-06-22).
            global_state.update_stats("metrics", metrics)

            # Calcul des moyennes de la ferme pour l'historique de performance
            if agent_metrics:
                farm_cpu = sum([d.get('cpu', 0) for d in agent_metrics.values()]) / len(agent_metrics)
                farm_mem = sum([d.get('ram', {}).get('UsedPercent', 0) for d in agent_metrics.values()]) / len(agent_metrics)
                farm_sessions = sum([d.get('sessions', {}).get('Total', 0) for d in agent_metrics.values()])
            else:
                farm_cpu = cpu
                farm_mem = mem.percent
                farm_sessions = 0

            perf_history = global_state.get_stats()["performance_history"]
            perf_history.append({
                "timestamp": datetime.now().isoformat(),
                "cpu": farm_cpu,
                "memory": farm_mem,
                "sessions": farm_sessions
            })
            global_state.update_stats("performance_history", perf_history)

            servers = cache.get("rds_servers") or get_rd_servers()
            cache.set("rds_servers", servers, ttl=300)
            global_state.update_stats("servers", servers)

            sessions = cache.get("rds_sessions") or get_all_rds_sessions()
            cache.set("rds_sessions", sessions, ttl=15)
            global_state.update_stats("sessions", sessions)
            global_state.update_stats("last_update", time.time())
            stats = global_state.get_stats()
            global_state.update_stats("health_status", analyze_health(metrics, sessions, servers, stats["agent_metrics"]))

            # Log de sante periodique (toutes les 5 minutes environ)
            if int(time.time()) % 300 < 15:
                logger.info(f"Engine Health: {len(servers)} serveurs, {len(sessions)} sessions, {len(stats['agent_metrics'])} agents actifs.")

        except Exception as e:
            logger.error(f"Error in fetch_data_loop: {e}")
        time.sleep(15)

from flask import request, jsonify
from functools import wraps

def measure_performance(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        web_metrics = global_state.get_web_metrics()
        web_metrics['requests_total'] += 1
        web_metrics['active_connections'] += 1
        global_state.update_web_metrics('requests_total', web_metrics['requests_total'])
        global_state.update_web_metrics('active_connections', web_metrics['active_connections'])
        try:
            result = f(*args, **kwargs)
            response_time = time.time() - start_time
            web_metrics = global_state.get_web_metrics()
            web_metrics['response_times'].append(response_time)
            web_metrics['requests_by_endpoint'][request.endpoint] += 1
            global_state.update_web_metrics('response_times', web_metrics['response_times'])
            global_state.update_web_metrics('requests_by_endpoint', web_metrics['requests_by_endpoint'])
            return result
        except Exception as e:
            web_metrics = global_state.get_web_metrics()
            web_metrics['errors_total'] += 1
            global_state.update_web_metrics('errors_total', web_metrics['errors_total'])
            logger.error(f"Error in {request.endpoint}: {str(e)}")
            raise
        finally:
            web_metrics = global_state.get_web_metrics()
            web_metrics['active_connections'] -= 1
            global_state.update_web_metrics('active_connections', web_metrics['active_connections'])
    return decorated_function

def queue_agent_command(hostname, action, params):
    if not hostname or not action: return None
    hostname = hostname.upper().strip().split('.')[0]
    # Deleguer a l'AgentMaster via master_client
    from Web.core.master_client import queue_command as _mc_queue
    return _mc_queue(hostname, action, params)

def update_command_result(cmd_id, status, result_message):
    try:
        with sqlite3.connect(db.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE agent_commands 
                SET status = ?, completed_at = ?, result_message = ? 
                WHERE id = ?
            ''', (status, datetime.now().isoformat(), result_message, cmd_id))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error update_command_result: {e}")
        return False

def get_command_status(cmd_id):
    # Deleguer a l'AgentMaster via master_client
    from Web.core.master_client import get_command_status as _mc_status
    result = _mc_status(cmd_id)
    if result:
        return {
            "id": result.get("id", cmd_id),
            "status": result.get("status"),
            "result": result.get("result_message"),
            "timestamp": result.get("created_at")
        }
    return None

def start_engine():
    threading.Thread(target=fetch_data_loop, daemon=True).start()
    logger.info("SysAdminMaster Engine started.")

