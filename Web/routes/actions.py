from flask import Blueprint, jsonify, request
import os
import subprocess
import re
import uuid
import logging
from datetime import datetime
from Web.core.engine import global_state, queue_agent_command, SCRIPTS_DIR, get_command_status
from Web.core.settings import broker_host as _broker_host

actions_bp = Blueprint('actions', __name__)
logger = logging.getLogger(__name__)

# Liste blanche des actions que le Dashboard peut faire exécuter aux agents (SYSTEM).
# Toute action hors de cette liste est refusée côté serveur (defense in depth :
# ne pas se reposer uniquement sur le switch de l'agent). Audit sécu 2026-06-22.
ALLOWED_AGENT_ACTIONS = {
    'logoff', 'disconnect', 'shadow', 'screenshot', 'message',
    'get_profiles', 'get_profile_size', 'clean_profile', 'analyze_profile',
    'restart_agent', 'update_agent', 'scan_profiles_sizes', 'clean_downloads',
    # Reparations Office : DEUX actions nommees depuis la v2.28.0.
    # Une panne d'authentification Office a deux causes distinctes — jeton SCA
    # perime, ou cache d'identite corrompu — et tant qu'un seul geste etait
    # nomme, tout incident etait attribue au jeton (REX du 31/08/2026).
    'renew_office_license', 'repair_office_identity',
    # TROISIEME cause, nommee le 23/09/2026 : les caches de jetons du broker
    # Azure AD bouclent (AADSTS50196) alors que jeton ET identite sont sains.
    # Et le DIAGNOSTIC qui la rend lisible : sans lui, on repare a l'aveugle -
    # c'est ce qui s'est passe deux fois de suite sur un meme utilisateur.
    'repair_office_broker', 'diagnose_office_auth',
    # Conserve pour compatibilite : un agent ou un appel non encore migre.
    'repair_office_token',
}

@actions_bp.route('/api/actions', methods=['POST'])
def run_action():
    data = request.get_json() or {}
    action = data.get('action', '').strip()
    server = data.get('server', 'LOCAL').strip().upper().split('.')[0]

    if action not in ALLOWED_AGENT_ACTIONS:
        logger.warning(f"⛔ Action refusée (hors liste blanche): '{action}' sur {server}")
        return jsonify({"status": "error", "message": f"Action non autorisée : {action}"}), 403

    # 🔴 Une commande vers un hôte INCONNU était mise en file en silence.
    # `server` a pour défaut 'LOCAL' : trois appels de /profiles envoyaient
    # `target: '<broker>'` (clé jamais lue ici) et retombaient donc sur 'LOCAL',
    # un hôte qui n'existe pas. La commande dormait à vie, l'UI affichait
    # « Délai dépassé — voir les logs de l'agent » et envoyait chercher un
    # problème côté agent alors que rien n'y était jamais arrivé (27/08/2026).
    # Une cible qui n'a jamais heartbeaté est une ERREUR D'APPEL, pas une file
    # d'attente : on refuse tout de suite et on le dit.
    from Web.core.engine import global_state as _gs
    known = set((_gs.get_stats().get("agent_metrics") or {}).keys())
    if known and server not in known:
        logger.warning(f"⛔ Action '{action}' vers un hôte inconnu : '{server}' "
                       f"(connus : {', '.join(sorted(known))})")
        return jsonify({
            "status": "error",
            "message": (f"Hôte inconnu : « {server} ». Aucun agent de ce nom n'a jamais "
                        f"envoyé de télémétrie — la commande n'a pas été mise en file.")
        }), 400

    logger.info(f"⚡ Action demandée: {action} sur {server}")

    # 🔴 Deux conventions de forme cohabitent côté UI et l'agent n'en connaît
    # qu'une : `sessions.html` envoie ses paramètres À PLAT
    # ({action, sessionId, user, ...}) tandis que `profiles.html` les IMBRIQUE
    # ({action, params:{days, dry_run}}). Le corps entier était transmis tel quel
    # comme `params` : l'agent lisait donc `$params.dry_run` -> $null sur la forme
    # imbriquée et retombait sur son défaut `$true`. Conséquence : **NETTOYER
    # exécutait une simulation** puis annonçait « Nettoyage terminé — 0 élément
    # supprimé », et le seuil en jours de l'UI n'était jamais appliqué (toujours
    # 14). Constaté le 27/08/2026. On aplatit : les deux formes fonctionnent,
    # l'imbriquée primant sur le plat en cas de doublon.
    params = dict(data)
    if isinstance(data.get('params'), dict):
        params.update(data['params'])

    cmd_id = queue_agent_command(server, action, params)
    if cmd_id:
        return jsonify({
            "status": "success", 
            "commandId": cmd_id,
            "message": f"Action {action} transmise au serveur {server}"
        })
    return jsonify({"status": "error", "message": "Impossible de contacter l'agent"}), 500

@actions_bp.route('/api/command-status/<cmd_id>', methods=['GET'])
def api_command_status(cmd_id):
    status = get_command_status(cmd_id)
    if status:
        return jsonify(status)
    return jsonify({"error": "Commande inconnue"}), 404

@actions_bp.route('/api/shell', methods=['POST'])
def api_shell():
    import time as _time
    data = request.json
    command = data.get('command', '').strip()
    server = data.get('server', 'Broker')

    if not command: return jsonify({"error": "Commande vide"}), 400

    if server.upper() in ('BROKER', 'ALL', 'TOUS'):
        return jsonify({
            "status": "error",
            "error": ("[indisponible sous Linux] Cette opération exécute PowerShell sur le Broker. "
                      "Le Dashboard tourne sous Linux : sélectionner un serveur RDS spécifique dans le filtre.")
        }), 501

    target = server.split('.')[0].upper()
    from Web.core.engine import queue_agent_command, get_command_status

    cmd_id = queue_agent_command(target, 'run_ps_command', {'command': command})
    if not cmd_id:
        return jsonify({"status": "error", "error": f"Agent {target} hors ligne ou introuvable."}), 503

    deadline = _time.time() + 30
    status = None
    while _time.time() < deadline:
        _time.sleep(3)
        status = get_command_status(cmd_id)
        if status and status.get('status') in ('success', 'error', 'completed'):
            break

    if not status:
        return jsonify({"status": "error", "error": f"Timeout : pas de réponse de {target} après 30s."})

    ok = status.get('status') in ('success', 'completed')
    out = status.get('result') or ''
    if ok:
        return jsonify({"status": "success", "stdout": out, "stderr": ""})
    else:
        return jsonify({"status": "error", "error": out})

@actions_bp.route('/api/tools', methods=['POST'])
def run_tool():
    """
    Execute un outil de maintenance via l'agent du serveur cible.
    Delègue via la file de commandes et poll le résultat (60s max).
    """
    import time as _time
    import platform as _platform
    data = request.get_json() or {}
    script = data.get('script', '').strip()
    server = data.get('server', 'All').strip().upper()

    # Mapping nom UI → action agent
    TOOL_MAP = {
        'clear_temp':                   'clear_temp',
        'optimize_ram':                 'optimize_ram',
        'restart_spooler':             'restart_spooler',
        'flush_dns':                    'flush_dns',
        'maintenanceandoptimization.ps1':  'global_maintenance',
        'troubleshootinganddiagnostics.ps1': 'diagnose_rds',
        'securityandcompliance.ps1':    'audit_security',
        'userprofilemanagement.ps1':    'manage_profiles',
        'applicationmanagement.ps1':    'manage_apps',
    }

    action = TOOL_MAP.get(script.lower())
    if not action:
        return jsonify({"status": "error", "output": f"Outil inconnu : {script}"}), 400

    from Web.core.engine import global_state, queue_agent_command, get_command_status

    # Cible : serveur sélectionné ou le broker (SAM_BROKER_HOST) par défaut
    broker = _broker_host()
    if server in ('ALL', 'TOUS', '', 'BROKER'):
        target = broker
    else:
        target = server.split('.')[0].upper()

    # Vérifier que l'agent est en ligne
    agent_metrics = global_state.get_stats().get('agent_metrics', {})
    if target not in agent_metrics and target != broker:
        return jsonify({"status": "error", "output": f"Agent {target} hors ligne ou introuvable."}), 503

    cmd_id = queue_agent_command(target, action, {})
    if not cmd_id:
        return jsonify({"status": "error", "output": f"Impossible de joindre l'agent {target}."}), 503

    # Poll 60s
    deadline = _time.time() + 60
    status = None
    while _time.time() < deadline:
        _time.sleep(3)
        status = get_command_status(cmd_id)
        if status and status.get('status') in ('success', 'error', 'completed'):
            break

    if not status:
        return jsonify({"status": "error", "output": f"[TIMEOUT] Pas de réponse de {target} après 60s."})

    ok = status.get('status') in ('success', 'completed')
    msg = status.get('result') or status.get('message') or ('OK' if ok else 'Erreur inconnue')
    return jsonify({"status": "success" if ok else "error", "output": msg})


def _run_ps_script(script_path, args_list, timeout=120):
    """Execute un script PowerShell et retourne (success, output)."""
    import platform
    if platform.system() != 'Windows':
        return False, ("[indisponible sous Linux] Cette opération exécute PowerShell sur le Broker. "
                       "Le Dashboard tourne sous Linux : lancer ce script directement sur le Broker Windows.")
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path] + args_list
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')
        output = proc.stdout + proc.stderr
        success = proc.returncode == 0
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Timeout : l'operation a depasse la limite de temps."
    except Exception as e:
        return False, str(e)


@actions_bp.route('/api/admin/agent-action', methods=['POST'])
def admin_agent_action():
    """
    Installe / desinstalle / diagnostique un agent PowerShell sur un hote RDS.
    action : install | uninstall | diagnose | restart
    target : hostname FQDN ou 'all'
    """
    data = request.get_json() or {}
    action = data.get('action', '').strip().lower()
    target = data.get('target', '').strip()

    if not action or not target:
        return jsonify({"success": False, "error": "action et target requis"}), 400

    valid_actions = {'install', 'uninstall', 'diagnose', 'restart'}
    if action not in valid_actions:
        return jsonify({"success": False, "error": f"Action invalide. Valeurs : {valid_actions}"}), 400

    install_script = os.path.join(SCRIPTS_DIR, 'Install-SysAdminMaster-Agent.ps1')
    agent_script = os.path.join(SCRIPTS_DIR, 'SysAdminMaster-Agent.ps1')
    if not os.path.exists(install_script):
        return jsonify({"success": False, "error": f"Script introuvable : {install_script}"}), 500

    from Web.core.engine import FULL_CONFIG
    broker_host = _broker_host()

    if target.lower() == 'all':
        hosts = FULL_CONFIG.get('RDSServers', {}).get('SessionHosts', [])
        targets = [h.split('.')[0].upper() for h in hosts if h.split('.')[0].upper() != broker_host]
    else:
        t = target.split('.')[0].upper()
        if t == broker_host:
            return jsonify({"success": False, "error": f"Impossible de deployer l'agent sur le broker ({broker_host}) - il fait tourner l'AgentMaster."}), 400
        targets = [t]

    logger.info(f"Agent action via AgentMaster: {action} sur {targets}")

    # Sous Linux, WinRM indisponible → déléguer l'action via l'agent du broker (SYSTEM Kerberos).
    # Le broker (agent >= v2.9.1) reçoit la commande et l'exécute localement ou en Invoke-Command.
    import platform as _platform
    if _platform.system() != 'Windows':
        from Web.core.engine import FULL_CONFIG as _FC, queue_agent_command as _qac, get_command_status as _gcs
        import time as _time

        broker_short = _broker_host()

        lines = [f"[INFO] AgentMaster Linux — délégation vers {broker_short} (agent SYSTEM)"]
        output_parts = []
        all_ok = True

        for host in targets:
            if action == 'install':
                # orchestrate_bootstrap : le broker télécharge les scripts et Invoke-Command sur host
                params_extra = {'target': host}
                cmd_id = _qac(broker_short, 'orchestrate_bootstrap', params_extra)
                if not cmd_id:
                    lines.append(f"[ERREUR] Impossible de contacter l'agent {broker_short} pour orchestrer le bootstrap de {host}.")
                    all_ok = False
                    continue

                lines.append(f"[*] Bootstrap {host} délégué à {broker_short} (cmd={cmd_id[:8]}…). Attente du résultat...")
                logger.info(f"orchestrate_bootstrap pour {host} via {broker_short}, cmd={cmd_id}")

                # Polling jusqu'à 90s (l'agent poll toutes les 10s + Invoke-Command ~20s)
                deadline = _time.time() + 90
                status = None
                while _time.time() < deadline:
                    _time.sleep(4)
                    status = _gcs(cmd_id)
                    if status and status.get('status') in ('success', 'error', 'completed'):
                        break

                if not status:
                    lines.append(f"[TIMEOUT] Pas de réponse de {broker_short} après 90s.")
                    all_ok = False
                elif status.get('status') in ('success', 'completed'):
                    # get_command_status renvoie 'result' (alias result_message depuis master.py)
                    msg = status.get('result') or status.get('message') or status.get('output') or 'OK'
                    lines.append(f"[OK] {host} : {msg}")
                else:
                    msg = status.get('result') or status.get('message') or status.get('output') or 'Erreur inconnue'
                    lines.append(f"[ERREUR] {host} : {msg}")
                    msg_lower = msg.lower()
                    if any(kw in msg_lower for kw in ('n existe pas', 'winrm', 'authenticate', 'access', 'denied', 'kerberos', 'ntlm', 'refus', 'acc')):
                        lines.append(f"[INFO] {broker_short}$ n'a pas les droits admin sur {host}.")
                        lines.append(f"[INFO]   → GPO Restricted Groups : ajouter {broker_short}$ dans Administrators sur les RDS.")
                        lines.append(f"[INFO]   → Fallback : télécharger le script bootstrap depuis le bouton SCRIPT ci-dessus.")
                    all_ok = False

            elif action in ('uninstall', 'diagnose', 'restart'):
                # Actions supportées nativement par l'agent (v2.8.0+) via la queue
                if action == 'restart':
                    agent_action = 'restart_agent'
                else:
                    agent_action = action
                cmd_id = _qac(host, agent_action, {})
                if cmd_id:
                    lines.append(f"[*] Action {action} envoyée à {host} (cmd={cmd_id[:8]}…) — résultat dans 10-20s.")
                else:
                    lines.append(f"[ERREUR] {host} hors ligne ou action non reconnue.")
                    all_ok = False
            else:
                lines.append(f"[ERREUR] Action '{action}' non supportée depuis Linux.")
                all_ok = False

        return jsonify({"success": all_ok, "output": "\n".join(lines)})

    # Lire les scripts a envoyer a l'AgentMaster
    with open(install_script, 'r', encoding='utf-8') as f:
        script_content = f.read()
    agent_script_content = ''
    if os.path.exists(agent_script):
        with open(agent_script, 'r', encoding='utf-8') as f:
            agent_script_content = f.read()

    # Deleguer a l'AgentMaster (qui a l'acces WinRM aux hotes RDS depuis le broker, master sous Windows)
    from Web.core.master_client import deploy_agents
    result = deploy_agents(action, targets, script_content, agent_script_content)

    if result is None:
        return jsonify({"success": False, "output": "[ERREUR] AgentMaster injoignable - impossible de deleguer l'operation."}), 503

    # Formatter la sortie pour la console Dashboard
    lines = []
    for host, r in result.get('results', {}).items():
        status = 'OK' if r.get('success') else 'ERREUR'
        lines.append(f"[{host}] {status}\n{r.get('output', '').strip()}")

    output = "\n".join(lines)
    return jsonify({"success": result.get('success', False), "output": output})


@actions_bp.route('/api/admin/fleet-update', methods=['POST'])
def admin_fleet_update():
    """
    Met à jour tous les agents en ligne via la queue de commandes (modèle pull HTTP).
    - v2.8.0+ : envoie update_agent → l'agent télécharge la nouvelle version depuis /scripts
    - v2.7.6  : signale "bootstrap requis" (ne supporte pas update_agent)
    - hors ligne : ignoré
    Retourne {queued: [hosts], legacy: [hosts], offline: [hosts]}
    """
    from Web.core.engine import global_state, queue_agent_command
    from datetime import datetime as _dt

    def _ver_tuple(v):
        """Convertit '2.9.1' en (2, 9, 1) pour comparaison — sans dépendance externe."""
        try:
            return tuple(int(x) for x in str(v).split('.'))
        except Exception:
            return (0,)

    stats = global_state.get_stats()
    agent_metrics = stats.get("agent_metrics", {})
    now = _dt.now()

    queued, legacy, offline = [], [], []

    for hostname, data in agent_metrics.items():
        try:
            received_at = _dt.fromisoformat(data.get('received_at', now.isoformat()))
            age = int((now - received_at).total_seconds())
        except Exception:
            age = 999
        is_online = age < 120

        if not is_online:
            offline.append(hostname)
            continue

        ver_str = data.get('agent_version', data.get('version', ''))
        is_capable = bool(ver_str) and _ver_tuple(ver_str) >= (2, 8, 0)

        if is_capable:
            cmd_id = queue_agent_command(hostname, 'update_agent', {})
            if cmd_id:
                queued.append(hostname)
                logger.info(f"fleet-update: update_agent envoyé à {hostname} (v{ver_str}), cmd={cmd_id}")
            else:
                legacy.append(hostname)
        else:
            legacy.append(hostname)
            logger.info(f"fleet-update: {hostname} (v{ver_str or 'N/A'}) = legacy, bootstrap requis")

    lines = []
    if queued:
        lines.append(f"[*] Mise à jour lancée sur : {', '.join(sorted(queued))}")
        lines.append(f"[*] L'agent va redémarrer dans ~10s après téléchargement.")
    if legacy:
        lines.append(f"[!] Bootstrap manuel requis (v2.7.6) : {', '.join(sorted(legacy))}")
        lines.append(f"[!] Exécuter Bootstrap-Agent-FromMaster.ps1 sur ces hôtes.")
    if offline:
        lines.append(f"[.] Hors ligne (ignorés) : {', '.join(sorted(offline))}")

    success = len(queued) > 0
    return jsonify({
        "success": success,
        "output": "\n".join(lines) if lines else "Aucun agent en ligne.",
        "queued": sorted(queued),
        "legacy": sorted(legacy),
        "offline": sorted(offline)
    })


@actions_bp.route('/api/admin/bootstrap-script/<hostname>', methods=['GET'])
def admin_bootstrap_script(hostname):
    """
    Génère un script PowerShell autonome à exécuter manuellement (depuis un compte d'administration)
    pour bootstrapper l'agent sur le hostname cible.
    Fallback quand le déploiement automatique via le compte machine du broker est bloqué (GPO manquante).
    """
    from flask import Response
    from Web.core.engine import FULL_CONFIG

    hostname = hostname.upper().split('.')[0]
    agent_script   = os.path.join(SCRIPTS_DIR, 'SysAdminMaster-Agent.ps1')
    install_script = os.path.join(SCRIPTS_DIR, 'Install-SysAdminMaster-Agent.ps1')

    if not os.path.exists(install_script):
        return jsonify({"error": "Script introuvable"}), 500

    broker_url = FULL_CONFIG.get('AgentMaster', {}).get('Url', 'http://sam-master:5000/api/agent/metrics')
    if not broker_url.endswith('/api/agent/metrics'):
        broker_url = broker_url.rstrip('/') + '/api/agent/metrics'
    token = FULL_CONFIG.get('Security', {}).get('AgentCommunicationToken', 'CHANGE_ME__token_via_config_ou_env')

    with open(install_script, 'r', encoding='utf-8') as f:
        install_content = f.read().rstrip('\n')
    agent_content = ''
    if os.path.exists(agent_script):
        with open(agent_script, 'r', encoding='utf-8') as f:
            agent_content = f.read().rstrip('\n')

    script = f'''<#
.SYNOPSIS
    Bootstrap-{hostname}.ps1 - Installe l'agent SysAdminMaster sur {hostname}
.DESCRIPTION
    Script généré automatiquement par SysAdminMaster Dashboard.
    Exécuter depuis une session avec droits admin sur {hostname}
    (ex: un compte d'administration sur le broker ou directement sur {hostname}).
.NOTES
    powershell -ExecutionPolicy Bypass -File "Bootstrap-{hostname}.ps1"
#>

$TargetHost   = "{hostname}"
$BrokerUrl    = "{broker_url}"
$Token        = "{token}"
$InstallPath  = "C:\\Program Files\\SysAdminMaster"

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Bootstrap Agent SysAdminMaster sur $TargetHost"  -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

# --- Contenu du script agent (intégré) ---
$agentContent = @'
{agent_content}
'@

# --- Contenu du script installeur (intégré) ---
$installContent = @'
{install_content}
'@

# --- Credentials admin pour Invoke-Command ---
# Si la session courante n'a pas les droits admin sur $TargetHost,
# une boîte de dialogue demande les credentials (ex: CONTOSO\admin.jdoe).
$cred = $null
try {{
    Invoke-Command -ComputerName $TargetHost -ScriptBlock {{ hostname }} -ErrorAction Stop | Out-Null
    Write-Host "[OK] Session courante acceptée par $TargetHost (pas de credentials supplémentaires requis)" -ForegroundColor Green
}} catch {{
    Write-Host "[AUTH] Accès refusé avec la session courante — entrez les credentials admin pour $TargetHost" -ForegroundColor Yellow
    $cred = Get-Credential -Message "Credentials admin pour $TargetHost (ex: CONTOSO\\admin.jdoe)"
}}

# --- Écriture sur la cible via Invoke-Command ---
$icArgs = @{{ ComputerName = $TargetHost; ErrorAction = 'Stop' }}
if ($cred) {{ $icArgs['Credential'] = $cred }}
Invoke-Command @icArgs -ScriptBlock {{
    param($AgentContent, $BrokerUrl, $Token)

    $InstallPath = "C:\\Program Files\\SysAdminMaster"
    $taskName    = "SysAdminMasterAgent"

    if (-not (Test-Path $InstallPath)) {{ New-Item -Path $InstallPath -ItemType Directory -Force | Out-Null }}

    $AgentContent | Out-File -FilePath "$InstallPath\\SysAdminMaster-Agent.ps1" -Encoding UTF8 -Force

    $wrapper = @"
Set-Location '$InstallPath'
& '$InstallPath\\SysAdminMaster-Agent.ps1' -BrokerUrl '$BrokerUrl' -IntervalSeconds 10 -Token '$Token'
"@
    $wrapper | Out-File -FilePath "$InstallPath\\Run-Agent.ps1" -Encoding UTF8 -Force

    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {{ Stop-ScheduledTask $taskName -ErrorAction SilentlyContinue; Unregister-ScheduledTask $taskName -Confirm:$false }}

    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object {{ $_.CommandLine -like "*SysAdminMaster*" }} |
        ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}

    $action    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstallPath\\Run-Agent.ps1`""
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Hours 0)
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    Start-ScheduledTask -TaskName $taskName

    Write-Output "[OK] Agent installé et démarré sur $env:COMPUTERNAME (broker=$BrokerUrl)"
}} -ArgumentList $agentContent, $BrokerUrl, $Token

Write-Host ""
Write-Host "[OK] Bootstrap terminé sur $TargetHost" -ForegroundColor Green
Write-Host "     Vérifier le statut dans ~30s sur le Dashboard SysAdminMaster." -ForegroundColor Cyan
'''

    return Response(
        script,
        mimetype='text/plain; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="Bootstrap-{hostname}.ps1"'}
    )


@actions_bp.route('/api/admin/master-action', methods=['POST'])
def admin_master_action():
    """
    Installe / diagnostique / arrete l'AgentMaster sur le broker (master sous Windows).
    action : install | diagnose | stop
    """
    data = request.get_json() or {}
    action = data.get('action', 'diagnose').strip().lower()

    valid_actions = {'install', 'diagnose', 'stop'}
    if action not in valid_actions:
        return jsonify({"success": False, "error": f"Action invalide. Valeurs : {valid_actions}"}), 400

    install_script = os.path.join(SCRIPTS_DIR, 'Install-AgentMaster.ps1')
    if not os.path.exists(install_script):
        return jsonify({"success": False, "error": f"Script introuvable : {install_script}"}), 500

    from Web.core.engine import FULL_CONFIG
    broker_short = _broker_host()

    logger.info(f"Master action: {action} sur {broker_short}")

    ok, output = _run_ps_script(
        install_script,
        ['-ComputerName', broker_short, '-Action', action],
        timeout=180
    )

    return jsonify({"success": ok, "output": output})


@actions_bp.route('/api/admin/master-script', methods=['GET'])
def admin_master_script():
    """
    Genere et retourne un script PowerShell autonome a executer directement
    sur le broker (sans WinRM depuis le Dashboard).
    Le script contient master.py et config.json integres via here-strings @'...'@
    (single-quote = pas d'interpolation, pas besoin d'echapper $ ni backtick).
    """
    from flask import Response
    base_dir = os.path.normpath(os.path.join(SCRIPTS_DIR, '..'))
    master_py_path = os.path.join(base_dir, 'AgentMaster', 'master.py')
    config_path    = os.path.join(base_dir, 'Config', 'config.json')

    if not os.path.exists(master_py_path):
        return jsonify({"error": f"master.py introuvable : {master_py_path}"}), 500

    with open(master_py_path, 'r', encoding='utf-8') as f:
        master_content = f.read().rstrip('\n')
    with open(config_path, 'r', encoding='utf-8') as f:
        config_content = f.read().rstrip('\n')

    # @'...'@ : here-string single-quote PowerShell — aucun echappement necessaire
    # La fermeture '@ DOIT etre en colonne 0 (pas d'indentation)
    script = f'''<#
.SYNOPSIS
    Deploy-AgentMaster.ps1 - Script autonome a executer DIRECTEMENT sur le broker
.DESCRIPTION
    Deploie master.py, installe Flask si absent, cree une tache planifiee SYSTEM.
    Genere automatiquement par SysAdminMaster Dashboard.
.NOTES
    Execution : powershell -ExecutionPolicy Bypass -File Deploy-AgentMaster.ps1
#>

$InstallPath = "C:\\Program Files\\SysAdminMaster\\AgentMaster"
$TaskName    = "SysAdminMasterBroker"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  AgentMaster Deploy - SysAdminMaster v2.7.6"  -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --- Ecriture de master.py ---
if (-not (Test-Path $InstallPath)) {{
    New-Item -Path $InstallPath -ItemType Directory -Force | Out-Null
}}

$masterContent = @'
{master_content}
'@
$masterContent | Out-File -FilePath "$InstallPath\\master.py" -Encoding UTF8 -Force
Write-Host "[OK] master.py ecrit dans $InstallPath" -ForegroundColor Green

# --- Ecriture de config.json ---
$configDir = "C:\\Program Files\\SysAdminMaster\\Config"
if (-not (Test-Path $configDir)) {{
    New-Item -Path $configDir -ItemType Directory -Force | Out-Null
}}
$configContent = @'
{config_content}
'@
$configContent | Out-File -FilePath "$configDir\\config.json" -Encoding UTF8 -Force
Write-Host "[OK] config.json ecrit" -ForegroundColor Green

# --- Detection Python (chemin ABSOLU obligatoire pour tache SYSTEM) ---
function Find-Python {{
    # 1. Chemins absolus systeme (prioritaires - accessibles par SYSTEM)
    $sysCandidates = @(
        "C:\\Python313\\python.exe","C:\\Python312\\python.exe","C:\\Python311\\python.exe",
        "C:\\Python310\\python.exe","C:\\Python39\\python.exe","C:\\Python38\\python.exe",
        "C:\\Program Files\\Python313\\python.exe","C:\\Program Files\\Python312\\python.exe",
        "C:\\Program Files\\Python311\\python.exe","C:\\Program Files\\Python310\\python.exe",
        "C:\\Program Files (x86)\\Python312\\python.exe","C:\\Program Files (x86)\\Python311\\python.exe"
    )
    foreach ($p in $sysCandidates) {{
        if (Test-Path $p) {{
            try {{ $v = & $p --version 2>&1; if ($v -match "Python 3") {{ return $p }} }} catch {{ }}
        }}
    }}

    # 2. Scan du registre HKLM (installations systeme - accessibles par SYSTEM)
    try {{
        if (Test-Path "HKLM:\\SOFTWARE\\Python\\PythonCore") {{
            Get-ChildItem "HKLM:\\SOFTWARE\\Python\\PythonCore" | ForEach-Object {{
                $ip = (Get-ItemProperty "$($_.PSPath)\\InstallPath" -ErrorAction SilentlyContinue)."(default)"
                if ($ip) {{
                    $exe = Join-Path $ip "python.exe"
                    if (Test-Path $exe) {{
                        try {{ $v = & $exe --version 2>&1; if ($v -match "Python 3") {{ return $exe }} }} catch {{ }}
                    }}
                }}
            }}
        }}
    }} catch {{ }}

    # 3. Python Launcher (py.exe) -> obtenir chemin absolu via sys.executable
    try {{
        $pyExe = (Get-Command py -ErrorAction SilentlyContinue).Source
        if ($pyExe) {{
            $realPath = & $pyExe -3 -c "import sys; print(sys.executable)" 2>$null
            if ($realPath -and (Test-Path $realPath)) {{ return $realPath }}
        }}
    }} catch {{ }}

    # 4. Get-Command python -> resoudre en chemin absolu
    try {{
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -and (Test-Path $cmd.Source)) {{
            $v = & $cmd.Source --version 2>&1
            if ($v -match "Python 3") {{ return $cmd.Source }}
        }}
    }} catch {{ }}

    # 5. where.exe -> chemin absolu
    try {{
        $found = where.exe python 2>$null | Select-Object -First 1
        if ($found -and (Test-Path $found)) {{
            $v = & $found --version 2>&1
            if ($v -match "Python 3") {{ return $found }}
        }}
    }} catch {{ }}

    # 6. Scan HKCU (dernier recours - peut ne pas etre accessible a SYSTEM)
    try {{
        if (Test-Path "HKCU:\\SOFTWARE\\Python\\PythonCore") {{
            Get-ChildItem "HKCU:\\SOFTWARE\\Python\\PythonCore" | ForEach-Object {{
                $ip = (Get-ItemProperty "$($_.PSPath)\\InstallPath" -ErrorAction SilentlyContinue)."(default)"
                if ($ip) {{
                    $exe = Join-Path $ip "python.exe"
                    if (Test-Path $exe) {{
                        try {{ $v = & $exe --version 2>&1; if ($v -match "Python 3") {{ return $exe }} }} catch {{ }}
                    }}
                }}
            }}
        }}
    }} catch {{ }}

    return $null
}}

$python = Find-Python
if (-not $python) {{
    Write-Host "[INFO] Python 3 introuvable - tentative d'installation automatique..." -ForegroundColor Yellow

    # Essai 1 : winget (Windows Server 2019+ / Windows 10+)
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {{
        Write-Host "[winget] Installation de Python 3.12..." -ForegroundColor Cyan
        winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        # Recharger le PATH
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
        $python = Find-Python
    }}

    # Essai 2 : telechargement direct du MSI Python
    if (-not $python) {{
        Write-Host "[DL] Telechargement de Python 3.12 depuis python.org..." -ForegroundColor Cyan
        $installer = "$env:TEMP\\python-3.12.9-amd64.exe"
        try {{
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe" -OutFile $installer -UseBasicParsing
            Write-Host "[DL] Installation silencieuse..." -ForegroundColor Cyan
            Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1" -Wait
            Remove-Item $installer -Force -ErrorAction SilentlyContinue
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
            $python = Find-Python
        }} catch {{
            Write-Host "[DL] Echec du telechargement : $($_.Exception.Message)" -ForegroundColor Red
        }}
    }}

    if (-not $python) {{
        Write-Host ""
        Write-Host "[ERREUR] Impossible d'installer Python automatiquement." -ForegroundColor Red
        Write-Host "Installez Python 3 manuellement puis relancez ce script :" -ForegroundColor Yellow
        Write-Host "  winget install Python.Python.3.12" -ForegroundColor Cyan
        Write-Host "  # ou telecharger : https://www.python.org/downloads/" -ForegroundColor Cyan
        exit 1
    }}
}}
Write-Host "[Python] Chemin absolu : $python" -ForegroundColor Green
Write-Host "[Python] Version      : $(& $python --version 2>&1)" -ForegroundColor Green

# --- Flask ---
$flaskOk = & $python -c "import flask; print('ok')" 2>&1
if ($flaskOk -ne "ok") {{
    Write-Host "[pip] Installation de Flask..." -ForegroundColor Cyan
    & $python -m pip install flask --quiet
    Write-Host "[pip] Flask installe" -ForegroundColor Green
}} else {{
    Write-Host "[Flask] Deja installe" -ForegroundColor Green
}}

# --- Test de demarrage master.py (detecte les erreurs Python avant la tache SYSTEM) ---
Write-Host "[TEST] Verification de master.py (3 secondes)..." -ForegroundColor Cyan
$testProc = Start-Process -FilePath $python `
    -ArgumentList "`"$InstallPath\\master.py`"" `
    -WorkingDirectory $InstallPath `
    -PassThru -NoNewWindow `
    -RedirectStandardError "$env:TEMP\\amtest_err.txt" `
    -RedirectStandardOutput "$env:TEMP\\amtest_out.txt"
Start-Sleep -Seconds 3
if ($testProc.HasExited) {{
    $errOut = Get-Content "$env:TEMP\\amtest_err.txt" -ErrorAction SilentlyContinue
    $stdOut = Get-Content "$env:TEMP\\amtest_out.txt" -ErrorAction SilentlyContinue
    Write-Host "[ERREUR] master.py a quitte immediatemant (code: $($testProc.ExitCode))" -ForegroundColor Red
    if ($errOut) {{ $errOut | ForEach-Object {{ Write-Host "  ERR: $_" -ForegroundColor Red }} }}
    if ($stdOut) {{ $stdOut | ForEach-Object {{ Write-Host "  OUT: $_" -ForegroundColor Gray }} }}
    Write-Host "[STOP] Corrigez l'erreur ci-dessus avant de continuer." -ForegroundColor Red
    exit 1
}} else {{
    Write-Host "[OK] master.py demarre correctement (PID $($testProc.Id))" -ForegroundColor Green
    $testProc | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}}

# --- Regle pare-feu Windows ---
$fwRule = Get-NetFirewallRule -DisplayName "AgentMaster-5000" -ErrorAction SilentlyContinue
if (-not $fwRule) {{
    New-NetFirewallRule -DisplayName "AgentMaster-5000" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow -Profile Any | Out-Null
    Write-Host "[Firewall] Regle inbound TCP 5000 creee" -ForegroundColor Green
}} else {{
    Write-Host "[Firewall] Regle TCP 5000 deja presente" -ForegroundColor Green
}}

# --- Tache planifiee ---
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {{
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[Tache] Ancienne tache supprimee" -ForegroundColor Yellow
}}

$taskAction = New-ScheduledTaskAction -Execute $python -Argument "`"$InstallPath\\master.py`"" -WorkingDirectory $InstallPath
$trigger    = New-ScheduledTaskTrigger -AtStartup
$settings   = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Hours 0)
$principal  = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal -Description "AgentMaster SysAdminMaster - Broker heartbeat receiver" | Out-Null
Write-Host "[Tache] $TaskName creee (python = $python)" -ForegroundColor Green

Start-ScheduledTask -TaskName $TaskName
Write-Host "[Tache] Demarrage en cours..." -ForegroundColor Cyan

# --- Attente demarrage Flask (15 secondes max) ---
$started = $false
for ($i = 1; $i -le 15; $i++) {{
    Start-Sleep -Seconds 1
    $portCheck = netstat -ano | Select-String ":5000 " | Select-String "LISTEN"
    if ($portCheck) {{
        $started = $true
        Write-Host "[OK] AgentMaster ecoute sur le port 5000 (apres ${{i}}s)" -ForegroundColor Green
        break
    }}
    Write-Host "  ... attente ${{i}}/15s" -ForegroundColor Gray
}}

if (-not $started) {{
    Write-Host ""
    Write-Host "[WARN] Port 5000 inactif apres 15s - diagnostic en cours..." -ForegroundColor Yellow

    # Etat de la tache planifiee
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($taskInfo) {{
        Write-Host "  Code retour tache : $($taskInfo.LastTaskResult)" -ForegroundColor Yellow
        Write-Host "  Dernier demarrage : $($taskInfo.LastRunTime)" -ForegroundColor Yellow
    }}

    # Dernieres lignes du log
    $logPath = "$InstallPath\\..\\Logs\\agentmaster.log"
    if (Test-Path $logPath) {{
        Write-Host "  --- Dernieres lignes du log ---" -ForegroundColor Yellow
        Get-Content $logPath -Tail 20 | ForEach-Object {{ Write-Host "    $_" -ForegroundColor Gray }}
    }}

    Write-Host ""
    Write-Host "[ACTION] Verifiez le log    : $logPath" -ForegroundColor Yellow
    Write-Host "[ACTION] Etat de la tache   : Get-ScheduledTask -TaskName $TaskName" -ForegroundColor Yellow
    Write-Host "[ACTION] Test local manuel  : curl http://localhost:5000/api/health" -ForegroundColor Yellow
}}

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Deploiement termine !" -ForegroundColor Green
Write-Host "  Test local   : curl http://localhost:5000/api/health" -ForegroundColor Cyan
Write-Host "  Python utilis: $python" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Green
'''

    return Response(
        script,
        mimetype='text/plain; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="Deploy-AgentMaster.ps1"'}
    )
