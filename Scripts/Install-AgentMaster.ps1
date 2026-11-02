<#
.SYNOPSIS
    Installe/Diagnostique/Arrete le service AgentMaster sur un hote Windows (master Windows, herite)
.DESCRIPTION
    Deploy AgentMaster/master.py sur le broker via WinRM.
    Le broker doit avoir Python installe (il avait deja le Dashboard Flask).
    Cree une tache planifiee SYSTEM qui demarre au boot.
.NOTES
    Version: 1.0
    Actions: install, diagnose, stop
#>

param(
    [Parameter(Mandatory = $true)][string]$ComputerName,
    [string]$Action = "install",
    [string]$PythonPath = ""
)

$TaskName    = "SysAdminMasterBroker"
$InstallPath = "C:\Program Files\SysAdminMaster\AgentMaster"
$MasterScript = Join-Path $PSScriptRoot "..\AgentMaster\master.py"
$ConfigFile   = Join-Path $PSScriptRoot "..\Config\config.json"

# --- Lecture du master.py local ---
if (-not (Test-Path $MasterScript)) {
    Write-Host "[ERREUR] master.py introuvable : $MasterScript" -ForegroundColor Red
    exit 1
}
$MasterContent = Get-Content $MasterScript -Raw -Encoding UTF8

# --- Lecture de config.json local ---
$ConfigContent = ""
if (Test-Path $ConfigFile) {
    $ConfigContent = Get-Content $ConfigFile -Raw -Encoding UTF8
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  AgentMaster Installer v1.0" -ForegroundColor Cyan
Write-Host "  Action : $Action sur $ComputerName" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# ---- Cible locale ou distante ? ----
$IsLocal = ($ComputerName -eq "localhost" -or $ComputerName -eq $env:COMPUTERNAME)

$ScriptBlock = {
    param($MasterContent, $ConfigContent, $Action, $TaskName, $InstallPath, $PythonHint)

    function Find-Python {
        # Cherche python.exe dans les emplacements courants
        $candidates = @(
            $PythonHint,
            "python",
            "C:\Python312\python.exe",
            "C:\Python311\python.exe",
            "C:\Python310\python.exe",
            "C:\Python39\python.exe",
            "C:\Program Files\Python312\python.exe",
            "C:\Program Files\Python311\python.exe",
            "C:\Program Files\Python310\python.exe",
            "C:\Program Files (x86)\Python312\python.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
        ) | Where-Object { $_ -ne "" }

        foreach ($p in $candidates) {
            try {
                $ver = & $p --version 2>&1
                if ($ver -match "Python 3") {
                    return $p
                }
            } catch { }
        }
        return $null
    }

    # ---- DIAGNOSTIC ----
    if ($Action -eq "diagnose") {
        Write-Output "=== DIAGNOSTIC AgentMaster ==="

        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) {
            $state = $task.State
            $lastRun = (Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue).LastRunTime
            Write-Output "[Tache] $TaskName : $state (dernier demarrage : $lastRun)"
        } else {
            Write-Output "[Tache] $TaskName : NON INSTALLEE"
        }

        $port5000 = netstat -ano | Select-String ":5000 " | Select-String "LISTEN"
        if ($port5000) {
            Write-Output "[Port]  5000 : ECOUTE"
        } else {
            Write-Output "[Port]  5000 : FERME"
        }

        $python = Find-Python
        if ($python) {
            $ver = & $python --version 2>&1
            Write-Output "[Python] $python : $ver"
        } else {
            Write-Output "[Python] INTROUVABLE"
        }

        if (Test-Path "$InstallPath\master.py") {
            Write-Output "[Fichier] master.py : PRESENT dans $InstallPath"
        } else {
            Write-Output "[Fichier] master.py : ABSENT"
        }

        return
    }

    # ---- STOP ----
    if ($Action -eq "stop") {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Write-Output "[OK] Tache $TaskName arretee"
        } else {
            Write-Output "[INFO] Tache $TaskName non trouvee"
        }
        # Tuer le process python sur port 5000 si encore vivant
        $pid5000 = (netstat -ano | Select-String ":5000 " | Select-String "LISTEN" |
            ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1)
        if ($pid5000) {
            Stop-Process -Id $pid5000 -Force -ErrorAction SilentlyContinue
            Write-Output "[OK] Process python (PID $pid5000) tue"
        }
        return
    }

    # ---- INSTALL ----
    $python = Find-Python
    if (-not $python) {
        Write-Output "[ERREUR] Python 3 introuvable sur ce serveur. Installez Python 3 et relancez."
        exit 1
    }
    $pyVer = & $python --version 2>&1
    Write-Output "[Python] Utilise : $python ($pyVer)"

    # Verifier que Flask est installe, sinon l'installer
    $flaskCheck = & $python -c "import flask; print('ok')" 2>&1
    if ($flaskCheck -ne "ok") {
        Write-Output "[pip] Installation de Flask..."
        & $python -m pip install flask --quiet
        Write-Output "[pip] Flask installe"
    } else {
        Write-Output "[Flask] Deja installe"
    }

    # Creer le dossier d'installation
    if (-not (Test-Path $InstallPath)) {
        New-Item -Path $InstallPath -ItemType Directory -Force | Out-Null
    }

    # Ecrire master.py
    $MasterContent | Out-File -FilePath "$InstallPath\master.py" -Encoding UTF8 -Force
    Write-Output "[Fichier] master.py deploye dans $InstallPath"

    # Ecrire config.json si fourni
    if ($ConfigContent -ne "") {
        $configDir = "$InstallPath\..\..\Config"
        if (-not (Test-Path $configDir)) {
            New-Item -Path $configDir -ItemType Directory -Force | Out-Null
        }
        $ConfigContent | Out-File -FilePath "$configDir\config.json" -Encoding UTF8 -Force
        Write-Output "[Config] config.json deploye"
    }

    # Supprimer l'ancienne tache si elle existe
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "[Tache] Ancienne tache supprimee"
    }

    # Creer la nouvelle tache planifiee
    $action = New-ScheduledTaskAction `
        -Execute $python `
        -Argument "`"$InstallPath\master.py`"" `
        -WorkingDirectory $InstallPath

    $trigger  = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -RestartCount 999 `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0)

    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "AgentMaster SysAdminMaster - Recoit les heartbeats des agents RDS" | Out-Null

    # Demarrer immediatement
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2

    # Verifier que le port est ouvert
    $port = netstat -ano | Select-String ":5000 " | Select-String "LISTEN"
    if ($port) {
        Write-Output "[OK] AgentMaster demarre et ecoute sur le port 5000"
    } else {
        Write-Output "[WARN] Port 5000 pas encore visible, le processus peut mettre quelques secondes"
    }
    Write-Output "[OK] Installation terminee"
}

# Execution locale ou via WinRM
try {
    if ($IsLocal) {
        & $ScriptBlock -MasterContent $MasterContent -ConfigContent $ConfigContent `
            -Action $Action -TaskName $TaskName -InstallPath $InstallPath -PythonHint $PythonPath
    } else {
        Write-Host "[*] Connexion WinRM vers $ComputerName..." -ForegroundColor Cyan
        if (-not (Test-Connection -ComputerName $ComputerName -Count 1 -Quiet)) {
            Write-Host "[ERREUR] $ComputerName non accessible (ping)" -ForegroundColor Red
            exit 1
        }
        Invoke-Command -ComputerName $ComputerName -ScriptBlock $ScriptBlock `
            -ArgumentList $MasterContent, $ConfigContent, $Action, $TaskName, $InstallPath, $PythonPath `
            -ErrorAction Stop
    }
    Write-Host ""
    Write-Host "[OK] Operation '$Action' terminee sur $ComputerName" -ForegroundColor Green
} catch {
    Write-Host "[ERREUR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
