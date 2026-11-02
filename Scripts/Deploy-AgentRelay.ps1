<#
.SYNOPSIS
    Deploy-AgentRelay.ps1 - OPTIONAL transparent L7 relay: <relay host>:<Port> -> SysAdminMaster master.
.DESCRIPTION
    Only needed when the agents cannot reach the master directly (segmented network, proxy
    forced on IP literals, master hosted in another zone). Run it ONCE (admin/SYSTEM) on a
    Windows host that the agents can reach and that can reach the master - typically the
    RDS connection broker. It installs a stateless Python relay listening on <Port> that
    forwards every request verbatim to http://<MasterHost>:<Port> and returns the master's
    answer unchanged:

      agents --> <relay host>:<Port> (THIS relay) --> <MasterHost>:<Port> (master)

    Because the master's answer travels back, command pull (/api/agent/metrics) and command
    results (/api/agent/command-result) work end to end. The relay stores nothing.

    Why by NAME: agents that point to "<relay host>:5000" by host name are resolved DIRECT
    by WPAD / the IE proxy settings, whereas an IP literal may be sent to a proxy (WinINET
    bug). A relay lets existing agents keep their URL while the master moves.

.PARAMETER MasterHost
    Host name or IP of the master as seen from the relay host. Mandatory.

.PARAMETER Port
    Port shared by the relay (inbound) and the master. Default: 5000.

.NOTES
    Replaces the legacy SysAdminMasterBroker task (local master.py) with SysAdminMasterRelay.
#>
param(
    [Parameter(Mandatory = $true)][string]$MasterHost,
    [int]   $Port = 5000
)

$ErrorActionPreference = "Stop"
$InstallPath = "C:\Program Files\SysAdminMaster\Relay"
$TaskName    = "SysAdminMasterRelay"
$OldTask     = "SysAdminMasterBroker"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  SysAdminMaster Relay (relay -> master)"       -ForegroundColor Cyan
Write-Host "  Cible master : http://${MasterHost}:${Port}"          -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $InstallPath)) {
    New-Item -Path $InstallPath -ItemType Directory -Force | Out-Null
}

# --- relay.py (proxy transparent, sans etat) ---
$relay = @"
# -*- coding: utf-8 -*-
# SysAdminMaster Relay - transparent L7 proxy relay host -> master.
# Genere par Deploy-AgentRelay.ps1. Ne stocke rien.
import sys, urllib.request, urllib.error
from flask import Flask, request, Response

MASTER_BASE = "http://${MasterHost}:${Port}"

# Forcer le DIRECT (ignorer tout proxy IE/WinINET/env sur l'hote relais).
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

app = Flask(__name__)

PASS_HEADERS = ('X-Agent-Token', 'X-Master-Token', 'Content-Type')

@app.route('/', defaults={'p': ''}, methods=['GET', 'POST'])
@app.route('/<path:p>', methods=['GET', 'POST'])
def proxy(p):
    url = MASTER_BASE + '/' + p
    if request.query_string:
        url += '?' + request.query_string.decode('latin-1')
    data = request.get_data() if request.method == 'POST' else None
    req = urllib.request.Request(url, data=data, method=request.method)
    for h in PASS_HEADERS:
        v = request.headers.get(h)
        if v:
            req.add_header(h, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            ct = r.headers.get('Content-Type', 'application/json')
            return Response(body, status=r.status, content_type=ct)
    except urllib.error.HTTPError as e:
        # Renvoyer tel quel le code/erreur du master (401, 400, 404...).
        body = e.read()
        ct = e.headers.get('Content-Type', 'application/json')
        return Response(body, status=e.code, content_type=ct)
    except Exception as e:
        return Response('{"error":"relay unreachable: %s"}' % str(e).replace('"', "'"),
                        status=502, content_type='application/json')

if __name__ == '__main__':
    print("SysAdminMaster Relay -> %s (port ${Port})" % MASTER_BASE)
    app.run(host='0.0.0.0', port=${Port}, debug=False)
"@
$relay | Out-File -FilePath "$InstallPath\relay.py" -Encoding UTF8 -Force
Write-Host "[OK] relay.py ecrit ($InstallPath\relay.py)" -ForegroundColor Green

# --- Detection Python (reprend la logique de Deploy-AgentMaster.ps1) ---
function Find-Python {
    $candidates = @(
        "python", "python3",
        "C:\Python313\python.exe","C:\Python312\python.exe","C:\Python311\python.exe",
        "C:\Python310\python.exe","C:\Python39\python.exe",
        "C:\Program Files\Python313\python.exe","C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe","C:\Program Files\Python310\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )
    foreach ($p in $candidates) {
        try { $v = & $p --version 2>&1; if ($v -match "Python 3") { return $p } } catch { }
    }
    try {
        $found = where.exe python 2>$null | Select-Object -First 1
        if ($found -and (Test-Path $found)) {
            $v = & $found --version 2>&1
            if ($v -match "Python 3") { return $found }
        }
    } catch { }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "[ERREUR] Python 3 introuvable sur cet hote." -ForegroundColor Red
    Write-Host "  L'ancien master.py tournait en Python : s'il a ete desinstalle, reinstaller :" -ForegroundColor Yellow
    Write-Host "  winget install Python.Python.3.12" -ForegroundColor Cyan
    exit 1
}
Write-Host "[Python] $python ($(& $python --version 2>&1))" -ForegroundColor Green

# --- Flask ---
$flaskOk = & $python -c "import flask; print('ok')" 2>&1
if ($flaskOk -ne "ok") {
    Write-Host "[pip] Installation de Flask..." -ForegroundColor Cyan
    & $python -m pip install flask --quiet
} else {
    Write-Host "[Flask] Deja installe" -ForegroundColor Green
}

# --- Stopper l'ancien master local (liberer le port 5000) ---
$old = Get-ScheduledTask -TaskName $OldTask -ErrorAction SilentlyContinue
if ($old) {
    Write-Host "[*] Arret de l'ancien master local ($OldTask)..." -ForegroundColor Yellow
    Stop-ScheduledTask -TaskName $OldTask -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $OldTask -Confirm:$false
}
# Tuer tout python.exe qui ecoute encore master.py
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*master.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# --- Tache planifiee du relais ---
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*relay.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$action    = New-ScheduledTaskAction -Execute $python -Argument "`"$InstallPath\relay.py`"" -WorkingDirectory $InstallPath
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Hours 0)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "SysAdminMaster Relay - pont transparent vers le master Linux" | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

# --- Verification ---
$listening = netstat -ano | Select-String ":${Port} " | Select-String "LISTEN"
if ($listening) {
    Write-Host "[OK] Relais demarre - port ${Port} en ecoute" -ForegroundColor Green
} else {
    Write-Host "[WARN] Port ${Port} pas encore visible - revenir dans qq secondes" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[*] Test du pont (health du master Linux a travers le relais) :" -ForegroundColor Cyan
try {
    $h = Invoke-RestMethod -Uri "http://localhost:${Port}/api/health" -TimeoutSec 6
    Write-Host "    -> master Linux : $($h.service) status=$($h.status) agents=$($h.agents_total)" -ForegroundColor Green
} catch {
    Write-Host "    -> ECHEC : $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "       (verifier que cet hote atteint http://${MasterHost}:${Port})" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Relais en place. Les agents RDS pointant"      -ForegroundColor Green
Write-Host "  ${env:COMPUTERNAME}:${Port} remontent maintenant au master." -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
