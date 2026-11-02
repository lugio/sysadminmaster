from flask import Blueprint, jsonify, request
import os
import socket
import psutil
import json
import random
import logging
from datetime import datetime
from Web.core.engine import global_state, cache, measure_performance

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__)


def _read_version():
    """Version applicative, lue UNE fois depuis le fichier VERSION a la racine du repo.

    Source de verite unique (le projet a vecu avec 11 declarations divergentes :
    l'API annoncait 2.7.6 alors que l'agent tournait en 2.9.5). Repli 'unknown'
    plutot qu'une valeur en dur : une version fausse est pire qu'une version absente.
    """
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, 'VERSION'), 'r', encoding='utf-8') as f:
            return f.read().strip() or 'unknown'
    except Exception:
        logger.warning("Fichier VERSION illisible - /api/health renverra 'unknown'")
        return 'unknown'


APP_VERSION = _read_version()


def _local_name():
    """Nom court de l'hote du Dashboard. COMPUTERNAME est une variable Windows,
    absente sous Linux (portage 2026-06-22) -> repli sur le hostname systeme."""
    name = os.environ.get('COMPUTERNAME') or socket.gethostname()
    return name.upper().split('.')[0]


def _agent_age_seconds(agent):
    """Ancienneté du dernier heartbeat, en secondes — None si indéterminable.

    Le master conserve indéfiniment le dernier payload de chaque agent : tester
    la simple présence d'une entrée ne dit RIEN de sa vivacité. Sans cette
    mesure, une page affiche des données figées comme si elles étaient à jour.
    """
    ts = (agent or {}).get('received_at') or (agent or {}).get('timestamp')
    if not ts:
        return None
    try:
        return max(0.0, (datetime.utcnow() - datetime.fromisoformat(str(ts))).total_seconds())
    except (ValueError, TypeError):
        return None

@api_bp.route('/api/metrics')
@measure_performance
def api_metrics():
    target_server = request.args.get('server', 'Broker').upper().split('.')[0]
    local_name = _local_name()
    stats = global_state.get_stats()
    agent_metrics = stats.get("agent_metrics", {})
    
    # Mode ALL: agregee de toute la ferme
    if target_server == 'ALL':
        if agent_metrics:
            cpus = [d.get('cpu', 0) for d in agent_metrics.values()]
            rams = [d.get('ram', {}).get('UsedPercent', 0) for d in agent_metrics.values()]
            avg_cpu = sum(cpus) / len(cpus) if cpus else 0
            avg_ram = sum(rams) / len(rams) if rams else 0
            
            # Agregation des disques critiques
            all_disks = []
            for host, data in agent_metrics.items():
                for disk in data.get('disks', []):
                    disk_copy = disk.copy()
                    disk_copy['Drive'] = f"[{host}] {disk.get('Drive')}"
                    all_disks.append(disk_copy)
            
            # Trier par taux d'occupation pour montrer les plus critiques
            all_disks.sort(key=lambda x: x.get('UsedPercent', 0), reverse=True)

            return jsonify({
                "CPU": avg_cpu,
                "Memory": {"UsedPercent": avg_ram},
                "Uptime": f"{len(agent_metrics)} agents",
                "Disks": all_disks[:10], # Top 10 disques critiques
                "TargetServer": "Ferme (Agregee)",
                "timestamp": datetime.now().isoformat()
            })
        else:
            # Fallback sur metriques locales si pas d'agents
            metrics = stats.get("metrics", {})
            metrics["TargetServer"] = f"{local_name} (Local - Aucun Agent)"
            metrics["timestamp"] = datetime.now().isoformat()
            return jsonify(metrics)
    
    if target_server in ['BROKER', 'LOCALHOST', '127.0.0.1', local_name]:
        metrics = stats.get("metrics", {})
        metrics["TargetServer"] = f"{local_name} (Local)"
        metrics["timestamp"] = datetime.now().isoformat()
        return jsonify(metrics)
    
    # Correction: Normalisation du nom de serveur pour correspondre aux cles des agents (Short Name)
    target_short = target_server.split('.')[0].upper()
    agent_data = stats.get("agent_metrics", {}).get(target_short)
    
    if agent_data:
        return jsonify({
            "CPU": agent_data.get('cpu', 0),
            "Memory": agent_data.get('ram', {"UsedPercent": 0}),
            "Uptime": agent_data.get('uptime', 'N/A'),
            "Disks": agent_data.get('disks', []),
            "TargetServer": f"{target_server} (Agent)",
            "timestamp": agent_data.get('received_at')
        })
    
    return jsonify({"error": "Agent Requis", "CPU": 0, "Memory": {"UsedPercent": 0}, "Uptime": "Agent requis"}), 200

@api_bp.route('/api/sessions')
@measure_performance
def api_sessions():
    """Retourne la liste des sessions RDS agregees (Zero-WinRM)."""
    # On recupere les sessions pre-calculees par le moteur (fetch_data_loop)
    stats = global_state.get_stats()
    sessions = stats.get("sessions", [])
    
    # Si le cache est vide (ex: demarrage), on force une aggregation immediate
    if not sessions:
        from Web.core.engine import get_all_rds_sessions
        sessions = get_all_rds_sessions()
        global_state.update_stats("sessions", sessions)
        
    return jsonify(sessions)

@api_bp.route('/api/user-detail')
@measure_performance
def api_user_detail():
    """Fiche detaillee d'un utilisateur de session : session + charge (v2.25.0)
    + jeton O365 + profil, assembles depuis le dernier heartbeat de l'hote.
    Chaque bloc est independant : absent = None, jamais un defaut deguise en
    donnee (lecon de l'audit des cles fantomes)."""
    user = (request.args.get('user') or '').strip()
    server = (request.args.get('server') or '').upper().split('.')[0]
    if not user or not server:
        return jsonify({"error": "user et server requis"}), 400

    stats = global_state.get_stats()
    data = stats.get("agent_metrics", {}).get(server)
    if not data:
        return jsonify({"error": f"Aucune donnee agent pour {server}"}), 404

    ukey = user.lower()
    received_at = data.get('received_at')

    session = None
    for s in (data.get('sessions', {}) or {}).get('List', []) or []:
        if isinstance(s, dict) and str(s.get('UserName', '')).lower() == ukey:
            session = s
            break

    load = None
    for ul in data.get('user_load') or []:
        if isinstance(ul, dict) and str(ul.get('user', '')).lower() == ukey:
            load = {"cpu": ul.get('cpu'), "mem_mb": ul.get('mem_mb'),
                    "procs": ul.get('procs'), "top": ul.get('top') or []}
            break

    o365 = None
    for u in (data.get('office_licenses', {}) or {}).get('users', []) or []:
        if isinstance(u, dict) and str(u.get('name', '')).lower() == ukey:
            o365 = {"status": u.get('status'), "age_days": u.get('age_days')}
            break

    profile = None
    for pr in data.get('user_profiles') or []:
        if isinstance(pr, dict) and str(pr.get('Name', pr.get('name', ''))).lower() == ukey:
            profile = pr
            break

    sess_summary = data.get('sessions', {}) or {}
    return jsonify({
        "user": user,
        "server": server,
        "received_at": received_at,
        "agent_version": data.get('agent_version'),
        "session": session,
        "load": load,
        "o365": o365,
        "profile": profile,
        # Contexte hote au meme heartbeat : situe la conso du user dans celle
        # de la machine ("7,8 Go" ne dit rien sans le total de l'hote).
        "host": {
            "cpu": data.get('cpu'),
            "ram_percent": (data.get('ram', {}) or {}).get('UsedPercent'),
            "ram_total_gb": (data.get('ram', {}) or {}).get('TotalGB'),
            "sessions_total": sess_summary.get('Total'),
            "sessions_active": sess_summary.get('Active'),
            "os": (data.get('os', {}) or {}).get('Caption'),
            "uptime": data.get('uptime')
        }
    })


@api_bp.route('/api/servers')
@measure_performance
def api_servers():
    stats = global_state.get_stats()
    servers = stats.get("servers", [])
    agent_metrics = stats.get("agent_metrics", {})
    
    enriched_servers = []
    now = datetime.now()
    
    for srv in servers:
        srv_short = srv.split('.')[0].upper()
        agent_data = agent_metrics.get(srv_short)
        
        status = "offline"
        cpu = 0
        ram = 0
        
        if agent_data:
            last_seen = datetime.fromisoformat(agent_data.get('received_at', now.isoformat()))
            if (now - last_seen).total_seconds() < 120:
                status = "online"
            else:
                status = "stale"
            
            cpu = agent_data.get('cpu', 0)
            ram = agent_data.get('ram', {}).get('UsedPercent', 0)
        
        enriched_servers.append({
            "name": srv,
            "shortName": srv_short,
            "status": status,
            "cpu": cpu,
            "ram": ram
        })
        
    return jsonify(enriched_servers)

@api_bp.route('/api/health')
@measure_performance
def api_health():
    stats = global_state.get_stats()
    agent_metrics = stats.get("agent_metrics", {})
    online_agents = sum(1 for d in agent_metrics.values() if (datetime.now() - datetime.fromisoformat(d.get('received_at', ''))).total_seconds() < 120)
    
    return jsonify({
        "status": stats.get("health_status", "unknown"),
        "timestamp": datetime.now().isoformat(),
        "uptime": stats.get("metrics", {}).get("Uptime", "unknown"),
        "agents": {"total": len(agent_metrics), "online": online_agents},
        "memory_usage": psutil.virtual_memory().percent,
        "version": APP_VERSION
    })

@api_bp.route('/api/performance')
@measure_performance
def api_performance():
    stats = global_state.get_stats()
    return jsonify(list(stats.get("performance_history", [])))

def _get_latest_agent_version():
    """Lit $script:AgentVersion depuis le fichier agent sur disque."""
    import re as _re
    script = os.path.join(os.path.dirname(__file__), '..', '..', 'Scripts', 'SysAdminMaster-Agent.ps1')
    try:
        with open(script, encoding='utf-8') as f:
            for line in f:
                m = _re.search(r'\$script:AgentVersion\s*=\s*"([^"]+)"', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return 'N/A'

@api_bp.route('/api/agent/status')
@measure_performance
def api_agent_status():
    stats = global_state.get_stats()
    agent_metrics = stats.get("agent_metrics", {})
    agents_list = []
    online_count = 0
    now = datetime.now()
    latest_version = _get_latest_agent_version()

    for hostname, data in agent_metrics.items():
        received_at = datetime.fromisoformat(data.get('received_at', now.isoformat()))
        age_seconds = int((now - received_at).total_seconds())
        is_online = age_seconds < 120
        if is_online: online_count += 1

        agents_list.append({
            "hostname": hostname,
            "display_name": data.get('hostname_short', hostname),
            "status": "online" if is_online else "offline",
            "cpu": data.get('cpu', 0),
            "ram": data.get('ram', {}).get('UsedPercent', 0),
            "sessions": data.get('sessions', {"Total": 0}),
            "age_seconds": age_seconds,
            "agent_version": data.get('agent_version', data.get('version', 'N/A')),
            # ⚠️ La clé du heartbeat est `processes_top5` — on lisait `top_processes`,
            # qui n'existe pas : la liste était donc TOUJOURS vide et les badges de
            # processus de la table Télémétrie ne s'affichaient jamais. Même famille
            # de défaut que `os_caption` (cf. /api/sysinfo) : une clé fantôme et un
            # défaut silencieux qui se lit comme « il n'y a rien à montrer ».
            "processes_top5": data.get('processes_top5', data.get('top_processes', [])),
        })

    return jsonify({
        "total": len(agent_metrics),
        "online": online_count,
        "latest_version": latest_version,
        "agents": sorted(agents_list, key=lambda x: x['hostname'])
    })

@api_bp.route('/api/agent/history')
@measure_performance
def api_agent_history():
    """Historique CPU/RAM par hôte pour les graphes de /agents.

    Sans cet endpoint, les courbes ne pouvaient afficher que ce que le JS avait
    accumulé depuis l'ouverture de la page — et étaient pré-remplies de zéros
    artificiels, donc mensongères au chargement (cf. metrics_history côté master).
    """
    from Web.core.master_client import get_history
    try:
        minutes = max(5, min(int(request.args.get('minutes', 120)), 1440))
    except (TypeError, ValueError):
        minutes = 120
    return jsonify({"status": "ok", "minutes": minutes, "hosts": get_history(minutes)})


@api_bp.route('/api/broker-optimization', methods=['GET'])
@measure_performance
def api_broker_optimization():
    # Logic to generate smart recommendations
    recs = []
    
    # 1. SQL Check
    try:
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'sysadminmaster.db')
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path) / (1024*1024)
            if db_size > 100:
                recs.append({
                    "type": "warning", "title": "Base de donnees volumineuse", 
                    "message": f"La base SQLite atteint {db_size:.1f} Mo. Envisagez une compression (VACUUM)."
                })
    except: pass

    # 2. Log Check
    try:
        log_path = os.path.join(os.path.dirname(__file__), '..', '..', 'Logs', 'web_server.log')
        if os.path.exists(log_path):
            log_size = os.path.getsize(log_path) / (1024*1024)
            if log_size > 50:
                recs.append({
                    "type": "info", "title": "Rotation des logs conseillee",
                    "message": f"Le fichier web_server.log fait {log_size:.1f} Mo. L'archivage libererait de l'espace."
                })
    except: pass

    # 3. Agent Check
    stats = global_state.get_stats()
    metrics = stats.get("agent_metrics", {})
    stale_count = 0
    now = datetime.now()
    for h, data in metrics.items():
        try:
            last_seen = datetime.fromisoformat(data.get('received_at', now.isoformat()))
            if (now - last_seen).total_seconds() > 300:
                stale_count += 1
        except: continue
    
    if stale_count > 0:
        recs.append({
            "type": "critical", "title": "Agents hors-ligne detectes",
            "message": f"{stale_count} agents n'ont pas envoye de heartbeat depuis plus de 5 minutes."
        })

    return jsonify({
        "status": "success",
        "timestamp": datetime.now().isoformat(),
        "server_count": len(stats.get("servers", [])),
        "session_count": len(stats.get("sessions", [])),
        "recommendations": recs
    })

@api_bp.route('/api/sysinfo')
@measure_performance
def api_sysinfo():
    target_server = request.args.get('server', 'Broker').upper().split('.')[0]
    local_name = _local_name()
    stats = global_state.get_stats()
    
    if target_server == 'ALL':
        agent_metrics = stats.get("agent_metrics", {})
        servers = stats.get("servers", [])
        return jsonify({
            "ComputerName": "Parc RDS Centralise",
            "OS": f"Ferme de {len(servers)} serveurs",
            "Version": f"{len(agent_metrics)} agents actifs",
            "LastBoot": "Audit Global",
            "status": "success"
        })

    if target_server in ['BROKER', 'LOCALHOST', '127.0.0.1', local_name]:
        # Info réelle du Broker
        import platform
        return jsonify({
            "ComputerName": local_name,
            "OS": f"{platform.system()} {platform.release()}",
            "Version": platform.version(),
            "LastBoot": "Broker Master",
            "status": "success"
        })
    
    agent_data = stats.get("agent_metrics", {}).get(target_server)
    if agent_data:
        # ⚠️ L'agent envoie l'OS dans un sous-objet `os` : {Caption, Version, LastBoot}.
        # Le code lisait des clés PLATES (`os_caption`, `os_version`) qui n'ont jamais
        # existé : les valeurs de repli ("Windows Server Agent", "N/A") s'affichaient
        # donc TOUJOURS, alors que la vraie donnée était disponible. Et `LastBoot`
        # recevait l'`uptime` (une durée, "0d 0h 18m") sous un libellé « Dernier
        # démarrage » (une date) — deux notions différentes. Constaté le 31/07/2026.
        os_info = agent_data.get('os') or {}
        return jsonify({
            "ComputerName": target_server,
            "OS": os_info.get('Caption') or 'Windows Server (agent)',
            "Version": os_info.get('Version') or 'N/A',
            "LastBoot": os_info.get('LastBoot') or 'N/A',
            "Uptime": agent_data.get('uptime') or 'N/A',
            "status": "success"
        })

    return jsonify({"error": "Agent non trouve", "status": "error"}), 404

# Libellés lisibles des services RDS suivis. Clé = nom réel du service Windows
# (celui que l'agent remonte), valeur = intitulé affiché. Une seule source pour
# la vue Ferme ET la vue par serveur.
RDS_SERVICE_LABELS = {
    "TermService":  "Services Bureau à distance",
    "SessionEnv":   "Configuration Bureau à distance",
    "UmRdpService": "Redirection ports UserMode",
}

# Statut « en marche » : l'agent PowerShell renvoie du TEXTE ("Running"), les
# branches internes du dashboard un CODE Windows (4). Les deux doivent être compris.
_RUNNING_TEXT = {'running', 'started', 'en cours', 'démarré', 'demarre'}


def _normalize_service(svc):
    """Ramène un service au contrat unique {Name, DisplayName, Status(int)}."""
    name = svc.get('Name') or '?'
    raw = svc.get('Status')
    running = (raw == 4) or (str(raw).strip().lower() in _RUNNING_TEXT)
    return {
        "Name": name,
        "DisplayName": svc.get('DisplayName') or RDS_SERVICE_LABELS.get(name, name),
        "Status": 4 if running else 1,
        "StatusText": str(raw) if raw is not None else 'inconnu',
    }


@api_bp.route('/api/services')
@measure_performance
def api_services():
    target_server = request.args.get('server', 'Broker').upper().split('.')[0]
    local_name = _local_name()
    stats = global_state.get_stats()
    agent_metrics = stats.get("agent_metrics", {})

    if target_server == 'ALL':
        # Audit global des services critiques.
        # ⚠️ La liste doit correspondre aux NOMS RÉELS des services que l'agent
        # collecte (Scripts/SysAdminMaster-Agent.ps1 : TermService, SessionEnv,
        # UmRdpService). L'ancienne liste auditait "RemoteDesktopConfig" (le vrai
        # nom est SessionEnv — RemoteDesktopConfig n'est que son libellé anglais)
        # et "BrokerService" (nom inventé, aucun service Windows ne s'appelle
        # ainsi) : ces deux lignes affichaient 0/10 EN ROUGE À VIE, ferme saine
        # ou pas — constaté le 31/07/2026, faux positif permanent depuis des mois.
        audit_results = []

        for svc_name, label in RDS_SERVICE_LABELS.items():
            display = f"{label} ({svc_name})"
            running_nodes = []
            down_nodes = []
            total_nodes = 0

            for host, data in agent_metrics.items():
                total_nodes += 1
                services = data.get('services', [])
                svc = next((s for s in services if s.get('Name') == svc_name), None)
                if svc and _normalize_service(svc)['Status'] == 4:
                    running_nodes.append(host)
                else:
                    down_nodes.append(host)

            if total_nodes > 0:
                percent = int((len(running_nodes) / total_nodes) * 100)
                audit_results.append({
                    "DisplayName": display,
                    "Name": svc_name,
                    "Status": "Audit",
                    "Ratio": f"{len(running_nodes)}/{total_nodes}",
                    "Percent": percent,
                    "NodesDown": down_nodes
                })

        return jsonify(audit_results)

    # Services pour le broker ou un agent spécifique
    if target_server in ['BROKER', 'LOCALHOST', '127.0.0.1', local_name]:
        import platform
        if platform.system() == 'Windows':
            return jsonify([
                {"Name": "TermService", "DisplayName": "Services Bureau a distance", "Status": 4},
                {"Name": "RemoteDesktopConfig", "DisplayName": "Configuration du Bureau a distance", "Status": 4},
                {"Name": "BrokerService", "DisplayName": "SysAdminMaster Broker Engine", "Status": 4}
            ])
        # Broker = hote Linux du Dashboard (portage 2026-06-22) : remonter les vrais
        # services SysAdminMaster plutot que des services Windows fictifs.
        from Web.core.master_client import get_master_health
        master_up = bool(get_master_health().get('reachable'))
        return jsonify([
            {"Name": "sam-dashboard", "DisplayName": "SysAdminMaster Dashboard (Flask)", "Status": 4},
            {"Name": "sam-agentmaster", "DisplayName": "SysAdminMaster AgentMaster (Broker Engine)", "Status": 4 if master_up else 1},
        ])
    
    agent_data = agent_metrics.get(target_server)
    if agent_data and 'services' in agent_data:
        # L'agent renvoie {Name, Status:"Running"} — SANS DisplayName et avec un
        # statut TEXTE, alors que les autres branches renvoient {Name, DisplayName,
        # Status:4} (code Windows numérique). Le payload brut était relayé tel quel :
        # l'UI affichait « undefined » comme nom et « ARRÊTÉ » pour des services qui
        # tournent (son test est `Status === 4`). Constaté le 31/07/2026 en basculant
        # le sélecteur de Ferme vers un serveur précis.
        # On NORMALISE ici, une fois, pour que tous les consommateurs voient le même
        # contrat — plutôt que d'apprendre à chaque template à gérer deux formes.
        return jsonify([_normalize_service(s) for s in agent_data['services']])

    return jsonify([])

@api_bp.route('/api/processes')
@measure_performance
def api_processes():
    target_server = request.args.get('server', 'Broker').upper().split('.')[0]
    stats = global_state.get_stats()
    agent_metrics = stats.get("agent_metrics", {})

    if target_server == 'ALL':
        # Top processus globaux agreges
        all_procs = []
        for host, data in agent_metrics.items():
            top_procs = data.get('processes_top5', [])
            for p in top_procs:
                p_copy = p.copy()
                p_copy['Name'] = f"[{host}] {p.get('Name')}"
                p_copy['Server'] = host
                all_procs.append(p_copy)
        
        # Trier par CPU decroissant et prendre le top 20
        all_procs.sort(key=lambda x: x.get('CPU', 0), reverse=True)
        return jsonify(all_procs[:20])

    if target_server == 'BROKER':
        processes = []
        # Utiliser psutil pour le broker local
        for proc in sorted(psutil.process_iter(['name', 'cpu_percent', 'memory_info']), 
                          key=lambda p: p.info['cpu_percent'] or 0, reverse=True)[:15]:
            try:
                processes.append({
                    "Name": proc.info['name'],
                    "CPU": proc.info['cpu_percent'] or 0,
                    "MemMB": round(proc.info['memory_info'].rss / (1024 * 1024), 1)
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied): continue
        return jsonify(processes)
    
    agent_data = agent_metrics.get(target_server)
    if agent_data:
        # L'agent envoie processes_top5 (v2.7.6)
        procs = agent_data.get('processes_top5', [])
        return jsonify(procs)
    
    return jsonify([])

@api_bp.route('/api/tasks/active')
@measure_performance
def api_tasks_active():
    # Les commandes sont gerees par l'AgentMaster, plus en local
    return jsonify({"count": 0, "tasks": []})

@api_bp.route('/api/master-status')
@measure_performance
def api_master_status():
    """Retourne le statut de connexion vers l'AgentMaster (health, latence, agents)."""
    from Web.core.master_client import get_master_health
    return jsonify(get_master_health())

@api_bp.route('/api/master-config-reload', methods=['POST'])
def api_master_config_reload():
    """Recharge config.json a chaud (utile apres changement d'IP)."""
    from Web.core.master_client import reload_config
    reload_config()
    from Web.core.master_client import get_master_health
    return jsonify({"status": "ok", "master": get_master_health()})

def _profiles_from_broker_agent():
    """Port Linux (2026-06-22) : le Dashboard ne lit plus le disque R: en local.
    La volumétrie R: et la liste des profils du broker remontent désormais via le
    heartbeat de l'agent du broker (`SAM_BROKER_HOST`, clés `disks` + `user_profiles`).

    Renvoie TOUJOURS un contrat valide `status:success` attendu par le front
    (disk + profiles + total_profiles), même si l'agent est hors-ligne ou si R:
    n'est pas remonté → un `note` explique l'état au lieu d'un toast d'erreur
    générique. Ne lève jamais d'exception.
    """
    from Web.core.master_client import get_agents
    from Web.core.settings import broker_host
    bname = broker_host()
    disk = {"total_gb": 0, "used_gb": 0, "free_gb": 0, "percent_used": 0}
    profiles = []
    note = None
    try:
        agents = get_agents() or {}
        broker = agents.get(bname) or agents.get(bname.lower())
        if not broker:
            return {"status": "success", "total_profiles": 0, "disk": disk,
                    "profiles": [], "note": f"Agent {bname} jamais vu — volumétrie R: indisponible.",
                    "agent_online": False, "agent_age_seconds": None}

        # 🔴 Fraîcheur du heartbeat, PAS seulement présence de la clé.
        # Le master conserve indéfiniment le dernier payload : `if not broker` ne
        # détecte donc qu'un agent JAMAIS vu, jamais un agent qui s'est tu. La page
        # affichait des données figées comme si elles étaient vivantes, et laissait
        # lancer des commandes vers un agent mort — elles restaient `pending` puis
        # l'UI conseillait « voir les logs de l'agent », alors que le vrai problème
        # était l'absence de heartbeat. Constaté le 31/07/2026 (agent du broker tombé).
        agent_age = _agent_age_seconds(broker)
        agent_online = agent_age is not None and agent_age < 120
        # « Occupé » n'est pas « mort » (v2.26.0). Un agent qui digère une file
        # ne heartbeatait plus et affichait la même bannière qu'un agent tombé :
        # 12 min de silence sur le broker le 27/08 alors qu'il travaillait. Il
        # annonce désormais son état ; on le relaie au lieu de le déclarer mort.
        agent_busy = broker.get('busy') or None
        if not agent_online:
            note = (f"Agent {bname} hors-ligne"
                    + (f" depuis {int(agent_age // 60)} min" if agent_age else "")
                    + " — données figées au dernier heartbeat, actions R: indisponibles.")
        elif agent_busy:
            note = (f"Agent {bname} occupé — {agent_busy.get('remaining')} commande(s) en file"
                    + (f" (en cours : {agent_busy.get('current')})" if agent_busy.get('current') else "")
                    + " — une nouvelle action sera traitée à la suite.")

        # Disque R: depuis les disques remontés par l'agent broker
        rdisk = None
        for d in (broker.get('disks') or broker.get('Disks') or []):
            if (d.get('Drive') or d.get('drive')) == 'R:':
                rdisk = d
                break
        if rdisk:
            total = float(rdisk.get('TotalGB') or 0)
            free = float(rdisk.get('FreeGB') or 0)
            disk = {
                "total_gb": round(total, 1),
                "used_gb": round(max(total - free, 0), 1),
                "free_gb": round(free, 1),
                "percent_used": round(float(rdisk.get('UsedPercent') or 0), 1),
            }
        else:
            note = f"Disque R: non remonté par l'agent {bname}."

        # Profils depuis user_profiles du heartbeat (SizeMB peut valoir "Scan requis")
        total_gb = disk["total_gb"] or 0
        source = None
        scanned_at = None
        adjusted_at = None
        for p in (broker.get('user_profiles') or []):
            name = p.get('Name') or p.get('name')
            if not name:
                continue
            source = source or p.get('Source')
            # Date du dernier AJUSTEMENT du cache apres un nettoyage (agent 2.26.3+).
            # Volontairement distincte de ScannedAt : les tailles ont ete corrigees
            # des Mo supprimes, mais le reste de l'arborescence n'a pas ete remesure.
            adjusted_at = adjusted_at or p.get('AdjustedAt')
            # Date de mesure des tailles : l'agent la remonte depuis 2.22, l'UI ne
            # l'affichait pas. Un volume sans sa date d'observation ne dit pas s'il
            # date de cette nuit ou d'il y a trois jours.
            scanned_at = scanned_at or p.get('ScannedAt')
            scan_required = False
            try:
                size_mb = float(p.get('SizeMB', p.get('size_mb')))
            except (TypeError, ValueError):
                size_mb, scan_required = 0.0, True
            percent_disk = round((size_mb / 1024 / (total_gb or 1)) * 100, 2) if size_mb else 0.0
            profiles.append({
                "name": name,
                "size_mb": round(size_mb, 1),
                "percent_disk": percent_disk,
                "last_modified": p.get('LastWriteTime') or p.get('last_modified') or 'N/A',
                "scan_required": scan_required,
                "size_label": "Scan requis" if scan_required else None,
            })
        profiles.sort(key=lambda x: x['size_mb'], reverse=True)
        # `source` = emplacement RÉELLEMENT énuméré par l'agent. Sans lui, l'UI
        # annonçait « Disque R: — N profils » en listant en fait C:\Users : la
        # volumétrie et la liste ne parlaient pas du même disque (cf. agent).
        # Agents < 2.19.0 ne l'envoient pas → on ne prétend rien dans ce cas.
        if source and disk["total_gb"] and not source.upper().startswith('R:'):
            note = (note or '') + (
                f" Liste énumérée depuis {source} — le disque R: affiché ci-dessus "
                "contient un autre jeu de dossiers (agent à mettre à jour).").strip()
        return {"status": "success", "total_profiles": len(profiles),
                "disk": disk, "profiles": profiles, "note": note,
                "agent_busy": agent_busy,
                "profiles_source": source, "sizes_scanned_at": scanned_at,
                "sizes_adjusted_at": adjusted_at,
                "sizes_pending": sum(1 for p in profiles if p["scan_required"]),
                "agent_online": agent_online,
                "agent_age_seconds": int(agent_age) if agent_age is not None else None}
    except Exception as e:
        logger.warning(f"get_profiles_data (port Linux/agent broker) a échoué: {e}")
        return {"status": "success", "total_profiles": 0, "disk": disk,
                "profiles": [], "note": "Volumétrie R: temporairement indisponible."}


def get_profiles_data():
    # Disque/profils du Broker (R:). Sous Windows : lecture locale historique.
    # Sous Linux (Dashboard porté sous Linux le 2026-06-22) : via l'agent du broker.
    import platform
    if platform.system() != 'Windows':
        return _profiles_from_broker_agent()
    try:
        total_gb = 0
        used_gb = 0
        free_gb = 0
        percent = 0
        
        # Test existence R:
        if os.path.exists('R:\\'):
            usage = psutil.disk_usage('R:\\')
            total_gb = round(usage.total / (1024**3), 1)
            used_gb = round(usage.used / (1024**3), 1)
            free_gb = round(usage.free / (1024**3), 1)
            percent = usage.percent
        else:
            # Fallback sur C: si R: n'existe pas (démo/dev)
            usage = psutil.disk_usage('C:\\')
            total_gb = round(usage.total / (1024**3), 1)
            used_gb = round(usage.used / (1024**3), 1)
            free_gb = round(usage.free / (1024**3), 1)
            percent = usage.percent

        # Liste des profils (Dossiers ou .vhdx)
        profiles = []
        path = 'R:\\' if os.path.exists('R:\\') else 'C:\\Users'
        
        try:
            items = os.listdir(path)
            # Exclure les fichiers système courants
            exclude = ['$RECYCLE.BIN', 'System Volume Information', 'Default', 'Public', 'desktop.ini']
            
            for item in items:
                if item in exclude or item.startswith('.'): continue
                
                try:
                    item_path = os.path.join(path, item)
                    if not os.path.exists(item_path): continue
                    
                    if os.path.isdir(item_path) or item.endswith('.vhdx'):
                        size_mb = 0
                        if os.path.isfile(item_path):
                            size_mb = os.path.getsize(item_path) / (1024*1024)
                        else:
                            # Simulation rapide de taille dossier pour éviter le scan bloquant
                            # Sur un vrai système on pourrait indexer les tailles en arrière-plan
                            size_mb = random.uniform(50, 2000)
                        
                        mtime = "N/A"
                        try:
                            mtime = datetime.fromtimestamp(os.path.getmtime(item_path)).isoformat()
                        except: pass
                        
                        profiles.append({
                            "name": item,
                            "size_mb": round(size_mb, 1),
                            "percent_disk": round((size_mb / 1024 / (total_gb or 1)) * 100, 2),
                            "last_modified": mtime
                        })
                except Exception as inner_e:
                    logger.warning(f"Erreur sur l'item {item}: {inner_e}")
                    continue
        except Exception as e:
            logger.error(f"Erreur listage profils: {e}")

        return {
            "status": "success",
            "total_profiles": len(profiles),
            "disk": {
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": free_gb,
                "percent_used": percent
            },
            "profiles": sorted(profiles, key=lambda x: x['size_mb'], reverse=True)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@api_bp.route('/api/alerts/status')
@measure_performance
def api_alerts_status():
    """État de la tâche d'alerte quotidienne + son dernier contrôle."""
    from Web.core.master_client import get_alert_status
    return jsonify({"status": "success", "alerts": get_alert_status()})


@api_bp.route('/api/alerts/history')
@measure_performance
def api_alerts_history():
    """Historique des contrôles, y compris ceux qui n'ont RIEN envoyé.

    C'est la raison d'être de la page : sans cette liste, « je ne reçois rien »
    ne se distingue pas d'une tâche cassée. Le 28/08/2026 il a fallu interroger
    l'API Mailjet pour établir que la tâche fonctionnait et qu'il n'y avait
    simplement rien à signaler ce jour-là.
    """
    from Web.core.master_client import get_alert_history
    try:
        limit = min(int(request.args.get('limit', 60)), 500)
    except (TypeError, ValueError):
        limit = 60
    return jsonify({"status": "success", "runs": get_alert_history(limit)})


@api_bp.route('/api/alerts/refresh-delivery', methods=['POST'])
def api_alerts_refresh_delivery():
    """Réinterroge Mailjet sur le sort RÉEL des messages déjà envoyés.

    Lecture externe uniquement : n'envoie rien, ne modifie que la colonne de
    statut. Un « success » à l'envoi ne prouve pas la livraison — c'est ce qui a
    masqué 24 jours de hard bounces entre le 31/07 et le 24/08/2026.
    """
    from Web.core.master_client import refresh_alert_delivery
    return jsonify({"status": "success", **refresh_alert_delivery()})


@api_bp.route('/api/alerts/test', methods=['POST'])
def api_alerts_test():
    """⚠️ ENVOI RÉEL d'un contrôle forcé aux destinataires configurés.

    🛡️ Listé dans `ALWAYS_BLOCK` du garde-fou e2e : un POST /api/* non catalogué
    passe par défaut dans prod-safe, et la QA clique tous les boutons — sans cette
    entrée, chaque run de la gate enverrait un vrai mail.
    """
    from Web.core.master_client import send_alert_test
    res = send_alert_test()
    if res.get('status') == 'ok':
        return jsonify({"status": "success", "sent": res.get('sent', False)})
    return jsonify({"status": "error",
                    "message": res.get('message') or "Le master n'a pas répondu"}), 502


@api_bp.route('/api/licenses')
@measure_performance
def api_licenses():
    """Dernier snapshot Office 365 par hote, CROISE avec la telemetrie live.

    Deux corrections de fond par rapport a la v2.9.0 :

    1. `license_history` ne garde qu'un point PAR CHANGEMENT (change-detection).
       Un hote muet conserve donc son dernier snapshot et l'UI l'affichait comme
       une donnee du jour : pendant les 38 jours de panne de juin, la page a
       montre des chiffres de juin sans aucun marqueur. On expose desormais la
       fraicheur reelle de l'agent (`agent_online`, `agent_age_seconds`) pour que
       l'UI puisse distinguer « mesure actuelle » de « derniere valeur connue ».

    2. Un hote SANS Office (broker, hote de test) n'a aucun dossier Licensing : tous
       ses profils etaient comptes « jeton absent » et gonflaient le total ferme
       en rouge. `office_installed` les sort des totaux.
    """
    from Web.core.master_client import get_licenses
    from Web.core import o365_scope
    hosts = o365_scope.annotate(get_licenses())

    # Perimetre demande. Defaut 'o365' : les comptes d'administration, de service
    # et les clients heberges sur la ferme ne sont pas des utilisateurs Office de
    # l'organisation, et les compter noie le signal (9 des 32 jetons perimes releves le
    # 01/09 etaient de ce type, dont 7 fois le MEME compte d'admin).
    # ⚠️ 'all' reste accessible : ecarter n'est pas cacher.
    scope = (request.args.get('scope') or 'o365').strip().lower()
    if scope not in ('o365', 'all'):
        scope = 'o365'

    # 🔴 Degradation SURE : tant qu'aucun agent ne remonte de DN (flotte < 2.29.0),
    # tout retombe en 'hors_ad' et le filtre masquerait la totalite des profils —
    # une page vide qui se lirait « aucun jeton a surveiller ». On sert alors
    # l'ensemble, et l'UI le DIT (dn_available=false). Ne jamais filtrer sur un
    # critere qu'on n'a pas mesure.
    dn_available = any(
        any((u or {}).get('dn') for u in (h.get('users') or []))
        for h in hosts
    )
    if not dn_available:
        scope = 'all'

    # Copie AVANT filtrage : les compteurs de perimetre decrivent l'ensemble.
    import copy
    all_hosts = copy.deepcopy(hosts)

    agent_metrics = global_state.get_stats().get("agent_metrics", {})
    now = datetime.now()

    totals = {"ok": 0, "stale": 0, "missing": 0}
    for h in hosts:
        name = (h.get("hostname") or "").upper()
        metrics = agent_metrics.get(name) or {}
        try:
            age = int((now - datetime.fromisoformat(metrics.get("received_at"))).total_seconds())
        except (TypeError, ValueError):
            age = None
        h["agent_age_seconds"] = age
        h["agent_online"] = age is not None and age < 120

        # 🔴 D — une reparation exige que le PROFIL soit monte, donc que
        # l'utilisateur ait une session sur CET hote (profils UPD). Sans cette
        # information, l'UI proposait un bouton qui ne pouvait pas aboutir et
        # rendait « Profil introuvable » 90 s plus tard (piege 24). On expose
        # donc les sessions ouvertes de l'hote pour que le bouton se grise.
        sess = (metrics.get("sessions") or {}).get("List") or []
        h["session_users"] = sorted({
            (x.get("UserName") or "").lower() for x in sess if isinstance(x, dict) and x.get("UserName")
        })

        # Les compteurs agreges envoyes par l'agent sont FAUX quand une categorie
        # ne contient qu'un seul utilisateur : en PowerShell, `(... | Where-Object
        # ...).Count` sur un resultat unique compte les CLES de la hashtable
        # (name/status/age_days = 3) au lieu des elements. Un hote avec 1 jeton
        # absent annoncait donc « 3 absents ». Corrige dans l'agent 2.13.0, mais on
        # recalcule ici depuis `users` — la liste brute, seule verite terrain — pour
        # que l'affichage soit juste AUSSI avec les agents anterieurs encore en place.
        # Filtrage de perimetre AVANT recomptage, pour que les totaux et la liste
        # portent sur le MEME ensemble (un ratio dont le numerateur et le
        # denominateur ne decrivent pas le meme perimetre ment).
        # 🔴 On ecarte ce qu'on SAIT hors perimetre, jamais ce qu'on ignore.
        # Un compte sans DN (agent < 2.29.0, ou personne pas revue depuis la mise
        # a jour) est INDETERMINE : le masquer reviendrait a filtrer sur un
        # critere non mesure — et c'est aussi ce que fait l'alerte, sans quoi la
        # page et le mail redonneraient deux chiffres differents.
        ECARTES = ('hors_perimetre', 'exclu', 'technique')
        if scope == 'o365' and isinstance(h.get("users"), list):
            h["users_hidden"] = sum(1 for u in h["users"]
                                    if (u or {}).get('scope') in ECARTES)
            h["users"] = [u for u in h["users"] if (u or {}).get('scope') not in ECARTES]
        else:
            h["users_hidden"] = 0

        users = h.get("users")
        if isinstance(users, list):
            for u in users:
                if isinstance(u, dict):
                    u["has_session"] = (u.get("name") or "").lower() in h["session_users"]
            counted = {"ok": 0, "stale": 0, "missing": 0}
            for u in users:
                status = (u or {}).get("status")
                if status in counted:
                    counted[status] += 1
            h["users_ok"], h["users_stale"], h["users_missing"] = (
                counted["ok"], counted["stale"], counted["missing"],
            )

        # 'not_installed' vient de l'agent >= 2.13.0 ; pour les agents anterieurs on
        # deduit l'absence d'Office de l'absence conjointe de version et de SCA.
        sca = (h.get("sca_status") or "").lower()
        h["office_installed"] = (
            sca != "not_installed"
            and (bool(h.get("office_version")) or sca in ("ok", "disabled"))
        )
        if not h["office_installed"]:
            continue

        totals["ok"]      += int(h.get("users_ok", 0))
        totals["stale"]   += int(h.get("users_stale", 0))
        totals["missing"] += int(h.get("users_missing", 0))

    return jsonify({
        "status": "ok", "hosts": hosts, "totals": totals, "scope": scope,
        "scope_ou": o365_scope.SCOPE_OU,
        "scope_exclude": o365_scope.SCOPE_EXCLUDE,
        # Nombre de profils ecartes par le perimetre, par categorie — pour que
        # l'UI puisse dire ce qu'elle ne montre pas plutot que de le taire.
        "scope_counts": o365_scope.counts(all_hosts),
        # Faux tant que la flotte n'est pas en agent >= 2.29.0 : sans DN,
        # tout le monde retombe en 'hors_ad' et le perimetre ne veut rien dire.
        "dn_available": dn_available,
    })

# Deux pannes distinctes, deux gestes nommes (v2.28.0). Voir le REX du
# 31/08/2026 : un jeton valide (7,9 j) coexistait avec une erreur
# d'authentification Office ; la cause etait le cache d'identite. Tant que
# l'UI ne nommait que "reparer le jeton", ce cas n'etait pas envisageable.
REPAIR_KINDS = {
    'license':  'renew_office_license',    # jeton SCA perime (> 30 j)
    'identity': 'repair_office_identity',  # cache d'identite corrompu (0x80070002)
    # 3e cause (23/09/2026, un utilisateur sur un hote de session) : les deux gestes
    # ci-dessus ont ete passes, tous deux "reussis", sans le moindre effet -
    # la boucle etait cote broker Azure AD (AADSTS50196). Tant que seules deux
    # causes etaient nommees, on ne pouvait que repeter ces deux gestes.
    'broker':   'repair_office_broker',    # caches OneAuth + TokenBroker
}


@api_bp.route('/api/licenses/repair', methods=['POST'])
def api_license_repair():
    """Lance une reparation Office sur un agent.

    Body JSON: {hostname, user, kind: 'license'|'identity'}
    Compatibilite : sans `kind`, on retombe sur l'action historique
    `repair_office_token` (+ include_identity), pour qu'un appel non migre
    continue de fonctionner pendant le deploiement de la flotte.
    """
    from Web.core.master_client import queue_command
    data = request.get_json() or {}
    hostname = (data.get('hostname') or '').upper().strip()
    user = (data.get('user') or '').strip()
    if not hostname or not user:
        return jsonify({"error": "hostname et user requis"}), 400

    kind = (data.get('kind') or '').strip().lower()
    if kind:
        action = REPAIR_KINDS.get(kind)
        if not action:
            return jsonify({"error": f"kind inconnu : {kind} (attendu : license | identity)"}), 400
        params = {"user": user}
    else:
        action = 'repair_office_token'
        params = {"user": user, "include_identity": bool(data.get('include_identity', False))}

    cmd_id = queue_command(hostname, action, params)
    if cmd_id:
        # ⚠️ Contrat de reponse fige : {status, command:{id}} — le garde-fou
        # e2e rend une reponse factice de cette forme exacte (Iron Law 1).
        return jsonify({"status": "queued", "command": {"id": cmd_id}})
    return jsonify({"error": "Impossible de contacter l'AgentMaster"}), 502

@api_bp.route('/api/licenses/diagnose', methods=['POST'])
def api_license_diagnose():
    """Lit le journal Azure AD de l'hote et rend le code d'erreur REEL.

    Body JSON: {hostname, user, hours?}
    LECTURE SEULE cote hote. Raison d'etre : jusqu'au 23/09/2026 la page ne
    savait afficher qu'un age de jeton, donc tout incident Office etait
    attribue au jeton. Ce n'est pas de l'inattention, c'est mecanique : un
    outil qui ne sait nommer qu'une cause fait diagnostiquer cette cause.
    """
    from Web.core.master_client import queue_command
    data = request.get_json() or {}
    hostname = (data.get('hostname') or '').upper().strip()
    user = (data.get('user') or '').strip()
    if not hostname or not user:
        return jsonify({"error": "hostname et user requis"}), 400
    try:
        hours = int(data.get('hours') or 6)
    except (TypeError, ValueError):
        hours = 6
    hours = max(1, min(hours, 24))

    cmd_id = queue_command(hostname, 'diagnose_office_auth', {"user": user, "hours": hours})
    if cmd_id:
        # Meme contrat de reponse que /repair (Iron Law 1 : le garde-fou e2e
        # rend une reponse factice de cette forme exacte).
        return jsonify({"status": "queued", "command": {"id": cmd_id}})
    return jsonify({"error": "Impossible de contacter l'AgentMaster"}), 502


@api_bp.route('/api/profiles-disk-info')
@measure_performance
def api_profiles_disk_info():
    return jsonify(get_profiles_data())

@api_bp.route('/api/profiles-disk-info-refresh', methods=['POST'])
def api_profiles_refresh():
    # Sur un vrai système, on pourrait forcer un recalcul de cache ici
    return jsonify(get_profiles_data())

@api_bp.route('/api/logs')
def get_logs():
    web_log = os.path.join(os.path.dirname(__file__), '..', 'web_server.log')
    logs = []
    if os.path.exists(web_log):
        with open(web_log, 'r', encoding='utf-8', errors='replace') as f:
            logs = f.readlines()[-100:]
    return jsonify({"file": "web_server.log", "content": logs})

# ===================== REPORTS API =====================

@api_bp.route('/api/reports/health')
@measure_performance
def api_reports_health():
    """Genere un rapport de sante des serveurs"""
    stats = global_state.get_stats()
    agent_metrics = stats.get("agent_metrics", {})
    servers = stats.get("servers", [])
    
    lines = []
    lines.append(f"<span style='color:#10b981'>▸ Infrastructure: {len(servers)} serveurs RDS configures</span>")
    lines.append(f"<span style='color:#10b981'>▸ Agents actifs: {len(agent_metrics)} heartbeats recus</span>")
    lines.append("")
    lines.append("<span style='color:#60a5fa; font-weight:bold;'>=== ETAT DES SERVEURS ===</span>")
    lines.append("")
    
    for srv in servers:
        srv_short = srv.split('.')[0].upper()
        agent = agent_metrics.get(srv_short, {})
        if agent:
            cpu = agent.get('cpu', 0)
            ram = agent.get('ram', {}).get('UsedPercent', 0)
            status = "<span style='color:#10b981'>● ONLINE</span>"
            lines.append(f"{status} {srv_short}: CPU {cpu:.1f}% | RAM {ram:.1f}%")
        else:
            lines.append(f"<span style='color:#f59e0b'>● STALE</span> {srv_short}: Aucune donnee recente")
    
    lines.append("")
    lines.append(f"<span style='color:#64748b'>Rapport genere a {datetime.now().strftime('%H:%M:%S')}</span>")
    
    return jsonify({"status": "success", "content": "<br>".join(lines)})

@api_bp.route('/api/reports/sessions')
@measure_performance
def api_reports_sessions():
    """Génère un rapport d'audit des sessions"""
    stats = global_state.get_stats()
    sessions = stats.get("sessions", [])
    servers = stats.get("servers", [])
    
    lines = []
    lines.append(f"<span style='color:#10b981'>▸ Sessions totales: {len(sessions)}</span>")
    
    active = [s for s in sessions if s.get('SessionState', '').lower() in ['active', 'actif']]
    disconnected = [s for s in sessions if s.get('SessionState', '').lower() in ['disconnected', 'déconnecté']]
    
    lines.append(f"<span style='color:#3b82f6'>▸ Sessions actives: {len(active)}</span>")
    lines.append(f"<span style='color:#f59e0b'>▸ Sessions déconnectées: {len(disconnected)}</span>")
    lines.append("")
    lines.append("<span style='color:#60a5fa; font-weight:bold;'>═══ RÉPARTITION PAR SERVEUR ═══</span>")
    lines.append("")
    
    for srv in servers:
        srv_short = srv.split('.')[0].upper()
        srv_sessions = [s for s in sessions if s.get('HostServer', '').upper().split('.')[0] == srv_short]
        srv_active = len([s for s in srv_sessions if s.get('SessionState', '').lower() in ['active', 'actif']])
        lines.append(f"  {srv_short}: {len(srv_sessions)} sessions ({srv_active} actives)")
    
    lines.append("")
    lines.append(f"<span style='color:#64748b'>Rapport généré à {datetime.now().strftime('%H:%M:%S')}</span>")
    
    return jsonify({"status": "success", "content": "<br>".join(lines)})

@api_bp.route('/api/reports/performance')
@measure_performance
def api_reports_performance():
    """Génère un rapport de performance"""
    stats = global_state.get_stats()
    metrics = stats.get("metrics", {})
    agent_metrics = stats.get("agent_metrics", {})
    perf_history = list(stats.get("performance_history", []))
    
    lines = []
    lines.append("<span style='color:#60a5fa; font-weight:bold;'>═══ MÉTRIQUES BROKER ═══</span>")
    lines.append(f"  CPU: {metrics.get('CPU', 0):.1f}%")
    lines.append(f"  RAM: {metrics.get('Memory', {}).get('UsedPercent', 0):.1f}%")
    lines.append("")
    
    lines.append("<span style='color:#60a5fa; font-weight:bold;'>═══ PERFORMANCE AGENTS ═══</span>")
    
    if agent_metrics:
        cpus = [d.get('cpu', 0) for d in agent_metrics.values()]
        rams = [d.get('ram', {}).get('UsedPercent', 0) for d in agent_metrics.values()]
        lines.append(f"  CPU moyen ferme: {sum(cpus)/len(cpus):.1f}%" if cpus else "  CPU moyen: N/A")
        lines.append(f"  RAM moyenne ferme: {sum(rams)/len(rams):.1f}%" if rams else "  RAM moyenne: N/A")
        lines.append(f"  Pic CPU: {max(cpus):.1f}%" if cpus else "")
    else:
        lines.append("  Aucune donnée agent disponible")
    
    lines.append("")
    lines.append("<span style='color:#60a5fa; font-weight:bold;'>═══ HISTORIQUE (dernières valeurs) ═══</span>")
    if perf_history:
        for entry in perf_history[-5:]:
            lines.append(f"  {entry.get('timestamp', 'N/A')[:19]}: CPU {entry.get('cpu', 0):.1f}% | RAM {entry.get('memory', 0):.1f}%")
    else:
        lines.append("  Pas d'historique disponible")
    
    lines.append("")
    lines.append(f"<span style='color:#64748b'>Rapport généré à {datetime.now().strftime('%H:%M:%S')}</span>")
    
    return jsonify({"status": "success", "content": "<br>".join(lines)})

@api_bp.route('/api/profile-action', methods=['POST'])
@measure_performance
def api_profile_action():
    """Exécute une action sur un profil du broker via l'agent du broker (`SAM_BROKER_HOST`).

    Port Linux 2026-06-22 : le Dashboard ne lit/écrit plus le disque en local,
    il délègue la commande à l'agent (analyze_profile / get_profile_size /
    clean_profile), puis le front interroge /api/command-status/<id>.
    L'ancienne implémentation était un STUB (command_id bidon + clé `profile`
    alors que le front envoie `name`) → toast générique "Erreur analyse: Inconnue".
    """
    data = request.get_json() or {}
    action = (data.get('action') or '').lower()
    # Le front envoie `name` ; on tolère `profile` par robustesse.
    profile = data.get('name') or data.get('profile') or ''
    # Page centralisée sur le broker : le front envoie 'LOCAL' (hérité Windows),
    # sans objet sous Linux -> on cible l'agent du broker.
    from Web.core.settings import broker_host
    server = data.get('server') or ''
    if server in ('', 'LOCAL', 'local'):
        server = broker_host()

    if not action or not profile:
        return jsonify({"status": "error", "message": "Champs requis manquants : action, name"}), 400

    # Mapping action front -> action agent (cf Scripts/SysAdminMaster-Agent.ps1)
    agent_action = {
        'analyze': 'analyze_profile',
        'size': 'get_profile_size',
        'clean': 'clean_profile',
        'cleanup': 'clean_profile',
    }.get(action)
    if not agent_action:
        return jsonify({"status": "error", "message": f"Action non supportée : {action}"}), 400

    from Web.core.engine import queue_agent_command
    cmd_id = queue_agent_command(server, agent_action, {"name": profile})
    if not cmd_id:
        return jsonify({"status": "error", "message": f"Impossible de contacter l'agent broker ({server} hors-ligne ?)"}), 502

    logger.info(f"Profile action: {action}->{agent_action} '{profile}' on {server} -> {cmd_id}")
    return jsonify({"status": "success", "command_id": cmd_id}), 200
