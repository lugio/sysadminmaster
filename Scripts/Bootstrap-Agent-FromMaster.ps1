<#
.SYNOPSIS
    Bootstrap / 1re bascule d'un agent SysAdminMaster vers le master.

.DESCRIPTION
    A executer SUR un hote RDS (en administrateur / SYSTEM), une seule fois, pour
    (re)installer l'agent COURANT servi par le master et le faire pointer sur lui.
    C'est la voie B de reprise de flotte (hote par hote, sans GPO). Une fois
    l'agent en place, les MAJ suivantes se font 100 % depuis Linux via
    l'action update_agent du tableau de bord (modele pull, sans WinRM).

    Le script :
      1. telecharge depuis le master  http://<MasterHost>:<Port>/scripts  (token-protege) :
         - SysAdminMaster-Agent.ps1        (agent courant)
         - Install-SysAdminMaster-Agent.ps1 (installeur)
         (telechargement DIRECT HttpWebRequest .Proxy=$null + parse-check bloquant)
      2. lance l'installeur en LOCAL avec le nouveau BrokerUrl -> recree la tache
         planifiee SysAdminMasterAgent pointant le master Linux.

.PARAMETER MasterHost
    Host name or IP of the master as seen from the session hosts. Default: sam-master.

.PARAMETER Port
    Port de l'AgentMaster. Defaut : 5000.

.PARAMETER Token
    X-Agent-Token (OBLIGATOIRE). Jamais code en dur ici.

.EXAMPLE
    # En local sur l'hote, en admin :
    .\Bootstrap-Agent-FromMaster.ps1 -Token '<agent-token>'

.EXAMPLE
    # Master designe par nom explicite :
    .\Bootstrap-Agent-FromMaster.ps1 -MasterHost sam-master.contoso.local -Port 5000 -Token '<agent-token>'

.NOTES
    Deployable en masse via GPO (script de demarrage) ou PsExec.
#>
param(
    [string]$MasterHost = "sam-master",
    [int]$Port = 5000,
    [Parameter(Mandatory = $true)][string]$Token,
    [int]$IntervalSeconds = 10
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Telechargement DIRECT via HttpWebRequest avec .Proxy=$null par requete.
# NE PAS revenir a Invoke-WebRequest + DefaultWebProxy=$null : en contexte
# SYSTEM/GPO le proxy WinINET/WPAD est relu par IWR et peut renvoyer un
# FAUX 200 (page HTML du proxy ecrite a la place du .ps1) — c'est le piege
# qui a masque la panne de flotte du 23/06/2026 (cf. agent Invoke-DirectHttp).
function Get-FileDirect {
    param([string]$Url, [string]$OutFile, [string]$AgentToken)
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.Method  = "GET"
    $req.Proxy   = $null
    $req.Timeout = 30000
    $req.Headers.Add("X-Agent-Token", $AgentToken)
    $resp = $req.GetResponse()
    try {
        $ms = New-Object System.IO.MemoryStream
        $resp.GetResponseStream().CopyTo($ms)
        [System.IO.File]::WriteAllBytes($OutFile, $ms.ToArray())
    } finally { $resp.Close() }
}

$base      = "http://${MasterHost}:${Port}"
$brokerUrl = "$base/api/agent/metrics"
$scriptsUrl= "$base/scripts"
$tmp       = Join-Path $env:TEMP ("sam-bootstrap-" + [System.Guid]::NewGuid().ToString("N").Substring(0,8))
$files     = @("SysAdminMaster-Agent.ps1", "Install-SysAdminMaster-Agent.ps1")

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Bootstrap agent -> $brokerUrl" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

New-Item -Path $tmp -ItemType Directory -Force | Out-Null

foreach ($f in $files) {
    Write-Host "[*] Telechargement $f ..." -ForegroundColor DarkGray
    Get-FileDirect -Url "$scriptsUrl/$f" -OutFile (Join-Path $tmp $f) -AgentToken $Token
}

# PARSE-CHECK BLOQUANT (Iron Law 5) : ne JAMAIS executer/installer un .ps1
# telecharge sans l'avoir parse dans le runtime local. Un fichier corrompu
# (proxy, tronquage, erreur de syntaxe cote master) executerait du code
# invalide et re-tuerait l'agent au moment meme de la reprise.
foreach ($f in $files) {
    $path = Join-Path $tmp $f
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$parseErrors)
    if ($parseErrors -and $parseErrors.Count -gt 0) {
        $detail = ($parseErrors | Select-Object -First 3 | ForEach-Object { $_.Message }) -join ' | '
        throw "Fichier telecharge INVALIDE ($f) : $($parseErrors.Count) erreur(s) de parsing. Bootstrap ABANDONNE, agent en place conserve. $detail"
    }
}
Write-Host "[*] parse-check OK (agent + installeur valides)" -ForegroundColor DarkGray

Write-Host "[*] Installation locale (BrokerUrl=$brokerUrl) ..." -ForegroundColor Cyan
& (Join-Path $tmp "Install-SysAdminMaster-Agent.ps1") `
    -ComputerName localhost `
    -BrokerUrl $brokerUrl `
    -Token $Token `
    -IntervalSeconds $IntervalSeconds

Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "[OK] Agent bascule sur $brokerUrl" -ForegroundColor Green
Write-Host "    Verifier cote master (X-Master-Token) : GET http://${MasterHost}:${Port}/api/master/agents" -ForegroundColor DarkGray
