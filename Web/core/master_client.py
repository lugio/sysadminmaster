"""
master_client.py - Client HTTP Dashboard -> AgentMaster

Toutes les erreurs sont catchees : degradation gracieuse (donnees stale plutot que page vide).
"""

import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (chargee une seule fois au premier appel)
# ---------------------------------------------------------------------------

_config = None

def _get_config():
    global _config
    if _config is not None:
        return _config

    config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'Config', 'config.json'))
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            full = json.load(f)
        _config = full.get('AgentMaster', {})
    except Exception as e:
        logger.error(f"master_client: impossible de lire config.json: {e}")
        _config = {}
    return _config


def reload_config():
    """Force le rechargement de la config (utile apres modification de config.json)."""
    global _config
    _config = None
    _get_config()


def _master_url():
    # Override env (migration Linux 2026-06-22) : pointer le Dashboard sur l'AgentMaster
    # local (ex. http://127.0.0.1:5000) sans éditer config.json. Prioritaire.
    env_url = os.environ.get('SAM_MASTER_URL')
    if env_url:
        return env_url.rstrip('/')
    cfg = _get_config()
    ip = cfg.get('Ip', '').strip()
    if ip:
        # Extraire le port depuis Url si present, sinon 5000 par defaut
        base_url = cfg.get('Url', 'http://sam-master:5000')
        try:
            port = base_url.split(':')[-1].rstrip('/')
            port = int(port)
        except (ValueError, IndexError):
            port = 5000
        return f"http://{ip}:{port}"
    return cfg.get('Url', 'http://sam-master:5000').rstrip('/')


def _master_token():
    return os.environ.get('SAM_MASTER_TOKEN') or _get_config().get('Token', '')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _request(method, path, data=None, timeout=10):
    """Execute une requete HTTP vers l'AgentMaster. Retourne le dict JSON ou None."""
    url = f"{_master_url()}{path}"
    headers = {
        'X-Master-Token': _master_token(),
        'Content-Type': 'application/json'
    }

    body = json.dumps(data).encode('utf-8') if data else None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        logger.warning(f"master_client {method} {path}: HTTP {e.code}")
    except urllib.error.URLError as e:
        logger.warning(f"master_client {method} {path}: connexion impossible ({e.reason})")
    except Exception as e:
        logger.warning(f"master_client {method} {path}: erreur inattendue ({e})")
    return None


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------

def get_agents():
    """GET /api/master/agents -> {hostname: data} ou {} si echec."""
    result = _request('GET', '/api/master/agents')
    if isinstance(result, dict) and 'error' not in result:
        return result
    return {}


def queue_command(hostname, action, params=None):
    """POST /api/master/command -> cmd_id (str) ou None."""
    payload = {
        'hostname': hostname,
        'action': action,
        'params': params or {}
    }
    result = _request('POST', '/api/master/command', data=payload)
    if result and 'commandId' in result:
        return result['commandId']
    return None


def get_command_status(cmd_id):
    """GET /api/master/command-status/<id> -> dict ou None."""
    result = _request('GET', f'/api/master/command-status/{cmd_id}')
    if result and 'error' not in result:
        return result
    return None


def deploy_agents(action, targets, script_content, agent_script_content=''):
    """
    POST /api/master/agent-deploy -> execute par le master (acces WinRM aux hotes RDS, master sous Windows).
    action: install|uninstall|diagnose|restart
    targets: list de hostnames courts
    Retourne dict {success, results: {host: {success, output}}} ou None si echec.
    """
    payload = {
        'action': action,
        'targets': targets,
        'script_content': script_content,
        'agent_script_content': agent_script_content
    }
    return _request('POST', '/api/master/agent-deploy', data=payload, timeout=300)


def get_licenses():
    """GET /api/master/licenses -> dict {status, hosts} ou None."""
    result = _request('GET', '/api/master/licenses')
    if result and result.get('status') == 'ok':
        return result.get('hosts', [])
    return []


def get_alert_status():
    """GET /api/master/alerts/status -> dict d'état de la tâche d'alerte."""
    result = _request('GET', '/api/master/alerts/status')
    if result and result.get('status') == 'ok':
        return result.get('alerts') or {}
    return {}


def get_alert_history(limit=60):
    """GET /api/master/alerts/history -> liste des contrôles (envoyés OU non)."""
    result = _request('GET', f'/api/master/alerts/history?limit={int(limit)}')
    if result and result.get('status') == 'ok':
        return result.get('runs') or []
    return []


def refresh_alert_delivery():
    """POST /api/master/alerts/refresh-delivery -> nombre de statuts mis à jour."""
    # 60 s : la route interroge Mailjet message par message, le défaut de 10 s
    # d'_request expirerait avant la fin sur un lot un peu large.
    result = _request('POST', '/api/master/alerts/refresh-delivery', timeout=60)
    if result and result.get('status') == 'ok':
        return {"updated": result.get('updated', 0), "checked": result.get('checked', 0)}
    return {"updated": 0, "checked": 0}


def send_alert_test():
    """POST /api/master/alerts/test -> ENVOI RÉEL d'un contrôle forcé."""
    return _request('POST', '/api/master/alerts/test', timeout=60) or {}


def get_history(minutes=120):
    """GET /api/master/history -> {hostname: [{t, cpu, ram, sessions}, ...]}."""
    result = _request('GET', f'/api/master/history?minutes={int(minutes)}')
    if result and result.get('status') == 'ok':
        return result.get('hosts', {})
    return {}


def get_master_health():
    """GET /api/health (sans auth) -> dict avec status, agents_total, agents_online, ou None.
    Inclut la latence de la requete et l'URL du master."""
    import time as _time
    url = _master_url()
    t0 = _time.time()

    # Appel direct sans header X-Master-Token (endpoint health est public)
    req = urllib.request.Request(f"{url}/api/health", method='GET')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            latency_ms = int((_time.time() - t0) * 1000)
            data['latency_ms'] = latency_ms
            data['url'] = url
            data['reachable'] = True
            return data
    except Exception as e:
        latency_ms = int((_time.time() - t0) * 1000)
        logger.warning(f"master_client health: {e}")
        return {
            'reachable': False,
            'url': url,
            'latency_ms': latency_ms,
            'error': str(e)
        }
