<#
.SYNOPSIS
    Installe l'agent SysAdminMaster - Version 2.32.0
.DESCRIPTION
    Deploie l'agent de telemetrie sur un ou plusieurs serveurs RDS.
    Cette version supporte le Shadowing RDS et les Live Snapshots.
    Date: 06/02/2026
#>

param(
    [string[]]$ComputerName = @("localhost"),
    [string]$BrokerUrl = "http://sam-master:5000/api/agent/metrics",
    [string]$Token = "CHANGE_ME__token_via_config_ou_env",
    [int]$IntervalSeconds = 10,
    [switch]$Uninstall
)

$ServiceName = "SysAdminMasterAgent"
$ServiceDisplayName = "SysAdminMaster Telemetry Agent"
$AgentScript = "$PSScriptRoot\SysAdminMaster-Agent.ps1"
$InstallPath = "C:\Program Files\SysAdminMaster"

function Install-AgentLocal {
    param([string]$Broker, [int]$Interval, [string]$TokenValue)
    
    Write-Host "[*] Installation de l'agent sur $env:COMPUTERNAME..." -ForegroundColor Cyan
    
    # Creer le dossier d'installation
    if (-not (Test-Path $InstallPath)) {
        New-Item -Path $InstallPath -ItemType Directory -Force | Out-Null
    }
    
    # Copier le script agent
    Copy-Item -Path $AgentScript -Destination "$InstallPath\SysAdminMaster-Agent.ps1" -Force
    
    # Creer le script wrapper pour le service
    $wrapperScript = @"
# SysAdminMaster Agent Service Wrapper
`$ErrorActionPreference = 'Continue'
Set-Location '$InstallPath'
& '$InstallPath\SysAdminMaster-Agent.ps1' -BrokerUrl '$Broker' -IntervalSeconds $Interval -Token '$TokenValue'
"@
    $wrapperScript | Out-File -FilePath "$InstallPath\Run-Agent.ps1" -Encoding UTF8 -Force
    
    # Supprimer l'ancien service si existant
    $existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existingService) {
        Write-Host "[*] Suppression de l'ancien service..." -ForegroundColor Yellow
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        sc.exe delete $ServiceName | Out-Null
        Start-Sleep -Seconds 2
    }
    
    # Creer le service avec NSSM ou via une tache planifiee (plus simple)
    Write-Host "[*] Creation de la tache planifiee..." -ForegroundColor Cyan
    
    $taskName = "SysAdminMasterAgent"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstallPath\Run-Agent.ps1`""
    
    # 🔴 Deux declencheurs, pas un. `-AtStartup` seul signifie qu'un agent mort
    # entre deux reboots ne revient JAMAIS tout seul : `-RestartCount` ne couvre
    # que les echecs, pas un arret par `schtasks /End`, et un hote RDS de production
    # redemarre rarement. Observed on a broker host — agent tombe,
    # aucune reprise a distance possible, intervention manuelle obligatoire.
    # Le declencheur repete est un filet : MultipleInstances=IgnoreNew (defaut)
    # fait qu'il ne demarre rien si l'agent tourne deja.
    $trigger = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
            -RepetitionInterval (New-TimeSpan -Minutes 15))
    )
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Hours 0)
    
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description "SysAdminMaster Telemetry Agent - Envoie les metriques au broker" | Out-Null
    
    # Demarrer immediatement
    Start-ScheduledTask -TaskName $taskName
    
    Write-Host "[OK] Agent installe et demarre sur $env:COMPUTERNAME" -ForegroundColor Green
    Write-Host "    Broker: $Broker" -ForegroundColor DarkGray
    Write-Host "    Intervalle: ${Interval}s" -ForegroundColor DarkGray
}

function Uninstall-AgentLocal {
    Write-Host "[*] Desinstallation de l'agent sur $env:COMPUTERNAME..." -ForegroundColor Yellow
    
    $taskName = "SysAdminMasterAgent"
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "[OK] Tache planifiee supprimee" -ForegroundColor Green
    }
    
    # Supprimer les fichiers
    if (Test-Path $InstallPath) {
        Remove-Item -Path $InstallPath -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[OK] Fichiers supprimes" -ForegroundColor Green
    }
    
    Write-Host "[OK] Agent desinstalle de $env:COMPUTERNAME" -ForegroundColor Green
}

# === MAIN ===
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  SysAdminMaster Agent Installer v2.32.0" -ForegroundColor Cyan
Write-Host "  Feature: Shadowing + Live Snapshots" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

foreach ($computer in $ComputerName) {
    if ($computer -eq "localhost" -or $computer -eq $env:COMPUTERNAME) {
        if ($Uninstall) {
            Uninstall-AgentLocal
        } else {
            # Nettoyer les processus existants avant reinstall locale (Get-CimInstance requis pour CommandLine en PS5.1)
            # Exclure le PID courant : le path de l'installeur contient aussi 'SysAdminMaster'
            $selfPid = $PID
            Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
                Where-Object { $_.CommandLine -like '*SysAdminMaster*' -and $_.ProcessId -ne $selfPid } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Install-AgentLocal -Broker $BrokerUrl -Interval $IntervalSeconds -TokenValue $Token
        }
    } else {
        Write-Host "[*] Deploiement distant sur $computer..." -ForegroundColor Cyan
        try {
            # Verifier que le script source existe localement avant de continuer
            if (-not (Test-Path $AgentScript)) {
                throw "Le script agent source est introuvable : $AgentScript"
            }

            # Tester la connectivite reseau (ICMP)
            if (-not (Test-Connection -ComputerName $computer -Count 1 -Quiet)) {
                throw "Serveur $computer non accessible via le reseau (Ping failed)"
            }

            # Utiliser Invoke-Command avec ErrorAction Stop pour capturer les echecs WinRM
            Invoke-Command -ComputerName $computer -ScriptBlock {
                param($AgentScriptContent, $Broker, $Interval, $Uninstall, $TokenValue)

                $InstallPath = "C:\Program Files\SysAdminMaster"
                $taskName = "SysAdminMasterAgent"

                if ($Uninstall) {
                    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                    if ($task) {
                        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
                    }
                    # Nettoyage processus
                    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | 
                        Where-Object { $_.CommandLine -like "*SysAdminMaster*" } | Stop-Process -Force -ErrorAction SilentlyContinue

                    if (Test-Path $InstallPath) {
                        Remove-Item -Path $InstallPath -Recurse -Force -ErrorAction SilentlyContinue
                    }
                    Write-Output "Agent desinstalle"
                } else {
                    # Creer le repertoire d'installation
                    if (-not (Test-Path $InstallPath)) {
                        New-Item -Path $InstallPath -ItemType Directory -Force | Out-Null
                    }

                    # Sauvegarder le script agent
                    $AgentScriptContent | Out-File -FilePath "$InstallPath\SysAdminMaster-Agent.ps1" -Encoding UTF8 -Force

                    # Wrapper script
                    $wrapperScript = @"
# SysAdminMaster Agent Service Wrapper
`$ErrorActionPreference = 'Continue'
Set-Location '$InstallPath'
& '$InstallPath\SysAdminMaster-Agent.ps1' -BrokerUrl '$Broker' -IntervalSeconds $Interval -Token '$TokenValue'
"@
                    $wrapperScript | Out-File -FilePath "$InstallPath\Run-Agent.ps1" -Encoding UTF8 -Force

                    # Supprimer ancienne tache et TUER processus
                    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                    if ($existingTask) {
                        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
                    }
                    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | 
                        Where-Object { $_.CommandLine -like "*SysAdminMaster*" -or $_.CommandLine -like "*Run-Agent.ps1*" } | Stop-Process -Force -ErrorAction SilentlyContinue

                    # Creer nouvelle tache
                    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
                        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstallPath\Run-Agent.ps1`""
                    # Deux declencheurs : AtStartup + repetition 15 min (filet
                    # anti-agent-mort, cf. incident on a broker host —
                    # `-RestartCount` ne couvre pas un arret par `schtasks /End`).
                    $trigger = @(
                        (New-ScheduledTaskTrigger -AtStartup),
                        (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
                            -RepetitionInterval (New-TimeSpan -Minutes 15))
                    )
                    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                        -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Hours 0)
                    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

                    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
                        -Settings $settings -Principal $principal -Description "SysAdminMaster Agent" | Out-Null

                    Start-ScheduledTask -TaskName $taskName
                    Write-Output "Agent installe et demarre"
                }
            } -ArgumentList (Get-Content $AgentScript -Raw), $BrokerUrl, $IntervalSeconds, $Uninstall, $Token -ErrorAction Stop

            Write-Host "[OK] $computer traite avec succes" -ForegroundColor Green
        } catch {
            $err = $_.Exception.Message
            if ($err -match "0x8009030e") {
                Write-Host "[ERREUR] $computer : Erreur Kerberos (0x8009030e). Verifiez l'identite du Broker ou utilisez TrustedHosts." -ForegroundColor Red
            } else {
                Write-Host "[ERREUR] $computer : $err" -ForegroundColor Red
            }
        }
    }
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Installation terminee" -ForegroundColor Green
Write-Host "  Verifiez le statut sur: $(($BrokerUrl -replace '/api/agent/metrics$', ''))/api/health" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
