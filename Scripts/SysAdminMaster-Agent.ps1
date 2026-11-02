#Requires -Version 5.1
<#
.SYNOPSIS
    SysAdminMaster Telemetry Agent - Agent leger de telemetrie pour serveurs RDS
.DESCRIPTION
    Envoie les metriques systeme au broker central toutes les 10 secondes via HTTP POST.
    Elimine le besoin de WinRM/CIM distant.
.NOTES
    Version: voir $script:AgentVersion ci-dessous (source de verite unique).
    Cet en-tete n'est PAS realigne par tools/check-version.py : la version de
    l'agent est un champ runtime, decouple du VERSION du depot depuis le 31/07/2026.
#>

param(
    [string]$BrokerUrl = "http://sam-master:5000/api/agent/metrics",
    [string]$Token = "CHANGE_ME__token_via_config_ou_env",
    [int]$IntervalSeconds = 10,
    [switch]$RunOnce,
    [switch]$Verbose,
    # Comptes techniques propres au site, ignores pour les jetons Office (en plus des comptes Windows standards)
    [string[]]$IgnoredAccounts = @()
)

# Configuration de l'encodage pour le support des accents (v2.7.6)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# Reseau (v2.8.2) : forcer TLS 1.2 et le DIRECT. En contexte SYSTEM, .NET route via
# le proxy IE/WinINET (distinct de WinHTTP, auto-detecte par WPAD). PROUVE 2026-06-22 :
# poser DefaultWebProxy=$null NE SUFFIT PAS - Invoke-RestMethod/Invoke-WebRequest
# relisent le proxy systeme et le POST part dans le proxy qui renvoie un faux 200
# (l'agent logge "envoye" mais le master ne recoit RIEN). Seul remede fiable : poser
# .Proxy=$null sur CHAQUE objet HttpWebRequest (cf. helper Invoke-DirectHttp). On garde
# la ligne ci-dessous comme defense en profondeur pour les autres appels .NET eventuels.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
[System.Net.WebRequest]::DefaultWebProxy = $null

# Configuration
$script:AgentVersion = "2.32.0"
$script:Hostname = $env:COMPUTERNAME
$script:LogFile = "$env:TEMP\SysAdminMaster-Agent.log"

function Write-AgentLog {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logLine = "[$timestamp] [$Level] $Message"
    Add-Content -Path $script:LogFile -Value $logLine -ErrorAction SilentlyContinue
    if ($Verbose) { Write-Host $logLine }
}

# --- Cache des tailles de profils -------------------------------------------
# Mesurer 101 arborescences prend ~37 s (mesure reelle sur R: le 31/07/2026) :
# impossible a chaque heartbeat (10 s), mais tres raisonnable une fois par nuit.
# Sans ce cache, la page affichait « SCAN REQUIS / 0,00 % » sur les 101 profils :
# le disque etait plein a 82 % et il aurait fallu 101 clics pour savoir qui
# consomme — la page ne pouvait pas repondre a sa propre question.
#
# 🔴 Le cache est PERSISTE sur disque, et ce n'est pas du confort.
# En v2.22 il ne vivait qu'en RAM : tout redemarrage de l'agent (fleet-update,
# restart_agent, reboot) reduisait les 101 profils a « SCAN REQUIS / 0,00 % »
# jusqu'au prochain passage a 3h. Constate le 31/07/2026 : deploiement a 19h23,
# page aveugle pour la nuit. Meme motif qu'un cache de monitoring perdu a chaque
# redeploiement. Une mesure qui coute 37 s doit survivre au processus qui l'a faite.
# ➡️ ProgramData et PAS $env:TEMP : l'agent expose une action `clear_temp` qui
#    viderait son propre cache — un nettoyeur ne doit pas pouvoir s'amputer.
$script:ProfileSizes   = @{}
$script:ProfileSizesAt = $null
# Horodatage du dernier AJUSTEMENT du cache (nettoyage des telechargements).
# Distinct de $script:ProfileSizesAt, et c'est delibere : apres un nettoyage on
# connait EXACTEMENT les Mo retires d'un profil, mais on n'a PAS remesure le
# reste de son arborescence. Ecraser la date de scan ferait passer une valeur
# derivee pour une mesure fraiche -- pire que le piege 17 : la valeur mentirait
# sur sa propre date. Les deux dates coexistent donc, et l'UI les distingue.
$script:ProfileSizesAdjustedAt = $null
$script:ProfileScanHour = 3          # scan automatique quotidien (heure locale)
$script:ProfileCacheFile = Join-Path $env:ProgramData 'SysAdminMaster\profile-sizes.json'

function Save-ProfileSizeCache {
    try {
        $dir = Split-Path $script:ProfileCacheFile -Parent
        if (-not (Test-Path $dir)) { New-Item -Path $dir -ItemType Directory -Force | Out-Null }
        @{
            scanned_at  = $script:ProfileSizesAt.ToString('yyyy-MM-dd HH:mm:ss')
            adjusted_at = if ($script:ProfileSizesAdjustedAt) { $script:ProfileSizesAdjustedAt.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
            sizes       = $script:ProfileSizes
        } | ConvertTo-Json -Depth 3 -Compress |
            Set-Content -Path $script:ProfileCacheFile -Encoding UTF8 -ErrorAction Stop
    }
    catch { Write-AgentLog "Save-ProfileSizeCache: $_" -Level "WARN" }
}

function Restore-ProfileSizeCache {
    try {
        if (-not (Test-Path $script:ProfileCacheFile)) { return }
        $raw = Get-Content -Path $script:ProfileCacheFile -Raw -Encoding UTF8 -ErrorAction Stop
        if (-not $raw) { return }
        $data = $raw | ConvertFrom-Json
        if (-not $data.scanned_at -or -not $data.sizes) { return }
        # ConvertFrom-Json rend un PSCustomObject, pas une hashtable : sans cette
        # reconstruction, ContainsKey() n'existe pas et la lecture echoue en silence.
        $h = @{}
        foreach ($p in $data.sizes.PSObject.Properties) { $h[$p.Name] = $p.Value }
        if ($h.Count -eq 0) { return }
        $script:ProfileSizes   = $h
        $script:ProfileSizesAt = [datetime]::ParseExact(
            $data.scanned_at, 'yyyy-MM-dd HH:mm:ss', $null)
        # Cache ecrit par un agent anterieur : le champ n'existe pas, on reste a $null.
        if ($data.PSObject.Properties.Name -contains 'adjusted_at' -and $data.adjusted_at) {
            try {
                $script:ProfileSizesAdjustedAt = [datetime]::ParseExact(
                    $data.adjusted_at, 'yyyy-MM-dd HH:mm:ss', $null)
            } catch { $script:ProfileSizesAdjustedAt = $null }
        }
        Write-AgentLog "Cache tailles profils restaure : $($h.Count) profil(s), mesure du $($data.scanned_at)" -Level "INFO"
    }
    catch { Write-AgentLog "Restore-ProfileSizeCache: $_" -Level "WARN" }
}

function Update-ProfileSizes {
    <#
    .SYNOPSIS
        Calcule la taille de chaque profil du stockage (R: en priorite, sinon C:\Users).
        Bloquant ~37 s pour 101 profils : quelques heartbeats sautes, l'agent reste
        « en ligne » (le seuil hors-ligne est a 120 s).
    #>
    try {
        $roots = @('R:\Redirections', 'R:\', 'C:\Users')
        $base = $null
        foreach ($r in $roots) { if (Test-Path $r) { $base = $r; break } }
        if (-not $base) { return }

        $exclude = @('Public','Default','Default User','All Users','Administrator')
        $sizes = @{}
        $dirs = @(Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -notin $exclude -and -not $_.Name.StartsWith('.') })
        foreach ($d in $dirs) {
            try {
                $sum = (Get-ChildItem $d.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                        Measure-Object Length -Sum).Sum
                $sizes[$d.Name] = if ($sum) { [math]::Round($sum / 1MB, 1) } else { 0 }
            } catch { }
        }
        $script:ProfileSizes   = $sizes
        $script:ProfileSizesAt = Get-Date
        # Un scan complet remesure tout : l'ajustement partiel devient caduc.
        $script:ProfileSizesAdjustedAt = $null
        Save-ProfileSizeCache
        Write-AgentLog "Scan tailles profils : $($sizes.Count) profil(s) mesure(s) depuis $base" -Level "INFO"
    }
    catch { Write-AgentLog "Update-ProfileSizes: $_" -Level "ERROR" }
}

# --- Nettoyage des dossiers de telechargement -------------------------------
# Action DESTRUCTIVE sur des donnees utilisateurs de PRODUCTION. Trois garde-fous,
# aucun negociable :
#   1. SIMULATION PAR DEFAUT ($DryRun = $true) : il faut passer explicitement
#      dry_run=false pour supprimer quoi que ce soit.
#   2. WHITELIST de noms de dossiers + verification que le chemin resolu est bien
#      SOUS la racine des profils. Un nom de dossier errone ne doit pas balayer
#      autre chose (et un nom inexistant ne doit pas "reussir" en silence).
#   3. CRITERE D'AGE PRUDENT : on exige que la date de creation ET la date de
#      modification soient anterieures au seuil. Un fichier recemment modifie
#      n'est jamais supprime, meme si sa date de creation est ancienne.
# Le dossier « Telechargement » lui-meme n'est JAMAIS supprime, seulement son contenu.
# 🔴 AUCUN ACCENT dans ce motif — et ce n'est pas de la coquetterie.
# Le script agent est servi en UTF-8 puis execute par PowerShell 5.1, qui le lit
# en CP1252 : un litteral 'Téléchargement' devient 'TÃ©lÃ©chargement' (codes
# 195,169 au lieu de 233) et ne matche JAMAIS le dossier reel — en silence.
# Measured on a broker host: 101 profiles scanned, 0 folder found.
# Meme famille que le tiret cadratin qui cassait le parse en v2.9.5.
# ➡️ On n'ecrit donc pas le nom : on ENUMERE les dossiers presents et on filtre
#    par un motif ASCII tolerant aux deux encodages (`.{1,2}` couvre 'é' comme
#    'Ã©'), ce qui accepte aussi 'Telechargement' sans accent.
$script:DownloadFolderPattern = '^T.{1,2}l.{1,2}chargements?$|^Downloads$'

function Invoke-DownloadsCleanup {
    param(
        [int]$Days = 14,
        [bool]$DryRun = $true,
        [string]$Profile = ''
    )

    $roots = @('R:\Redirections', 'R:\', 'C:\Users')
    $base = $null
    foreach ($r in $roots) { if (Test-Path $r) { $base = $r; break } }
    if (-not $base) { return @{ error = 'Aucun stockage de profils trouve' } }
    $baseFull = (Get-Item $base).FullName.TrimEnd('\')

    $cutoff = (Get-Date).AddDays(-[math]::Abs($Days))
    $exclude = @('Public','Default','Default User','All Users','Administrator')

    $profiles = @(Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -notin $exclude -and -not $_.Name.StartsWith('.') })
    if ($Profile) { $profiles = @($profiles | Where-Object { $_.Name -eq $Profile }) }

    $items = @()
    $totalBytes = 0
    $foldersSeen = 0

    foreach ($prof in $profiles) {
        $dlDirs = @(Get-ChildItem -Path $prof.FullName -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match $script:DownloadFolderPattern })
        foreach ($dlDir in $dlDirs) {
            $dl = $dlDir.FullName
            $foldersSeen++
            # Garde-fou 2 : le chemin resolu DOIT rester sous la racine des profils
            $dlFull = $dlDir.FullName
            if (-not $dlFull.StartsWith($baseFull, [StringComparison]::OrdinalIgnoreCase)) { continue }

            foreach ($it in @(Get-ChildItem -Path $dl -Force -ErrorAction SilentlyContinue)) {
                # Garde-fou 4 : ne JAMAIS toucher aux elements systeme.
                # La 1re simulation (31/07/2026) a remonte '$RECYCLE.BIN' parmi les
                # candidats — 1,5 Go chez un utilisateur. Supprimer le dossier de
                # corbeille par Remove-Item est sale (Windows le recree, et ce n'est
                # pas la bonne facon de vider une corbeille). Ecarte explicitement.
                if ($it.Name -like '$*' -or
                    $it.Name -eq 'desktop.ini' -or
                    $it.Name -eq 'Thumbs.db' -or
                    $it.Name -eq 'System Volume Information') { continue }

                # Garde-fou 3 : creation ET modification anterieures au seuil
                $newest = if ($it.LastWriteTime -gt $it.CreationTime) { $it.LastWriteTime } else { $it.CreationTime }
                if ($newest -ge $cutoff) { continue }
                $size = 0
                try {
                    if ($it.PSIsContainer) {
                        $s = (Get-ChildItem $it.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                              Measure-Object Length -Sum).Sum
                        $size = if ($s) { $s } else { 0 }
                    } else { $size = $it.Length }
                } catch { }
                $totalBytes += $size
                $items += [PSCustomObject]@{
                    Profile = $prof.Name
                    Path    = $it.FullName
                    Name    = $it.Name
                    SizeMB  = [math]::Round($size / 1MB, 1)
                    Age     = [int]((Get-Date) - $newest).TotalDays
                    Deleted = $false
                }
            }
        }
    }

    $deleted = 0; $failed = 0; $freed = 0
    if (-not $DryRun) {
        foreach ($it in $items) {
            try {
                Remove-Item -LiteralPath $it.Path -Recurse -Force -ErrorAction Stop
                # Verification post-suppression : `-ErrorAction SilentlyContinue`
                # ailleurs dans ce script a deja fait croire a des succes fictifs.
                if (Test-Path -LiteralPath $it.Path) { $failed++ }
                else { $it.Deleted = $true; $deleted++; $freed += $it.SizeMB }
            } catch { $failed++ }
        }
        Write-AgentLog "clean_downloads: $deleted supprime(s), $failed echec(s), $([math]::Round($freed/1024,2)) Go liberes (seuil $Days j)" -Level "INFO"

        # 🔴 Le cache des tailles ne se mettait PAS a jour apres un nettoyage.
        # La page affichait "X Go liberes" juste au-dessus d'un tableau montrant
        # les tailles d'AVANT, jusqu'au scan de 3h. Signale en conditions reelles
        # le 28/08/2026. On decremente ici les Mo REELLEMENT supprimes (seuls les
        # elements dont Test-Path a confirme la disparition, cf. $it.Deleted),
        # profil par profil. Pas de remesure complete : elle coute 37 s, ferait
        # depasser le delai de l'UI (90 s) et transformerait un nettoyage reussi
        # en "Delai depasse" -- exactement le message trompeur du piege 25.
        if ($deleted -gt 0 -and $script:ProfileSizesAt) {
            try {
                $freedByProfile = @{}
                foreach ($it in $items) {
                    if (-not $it.Deleted) { continue }
                    if (-not $freedByProfile.ContainsKey($it.Profile)) { $freedByProfile[$it.Profile] = 0 }
                    $freedByProfile[$it.Profile] += $it.SizeMB
                }
                $adjusted = 0
                foreach ($name in $freedByProfile.Keys) {
                    if (-not $script:ProfileSizes.ContainsKey($name)) { continue }
                    $before = $script:ProfileSizes[$name]
                    # Une entree peut valoir la chaine 'Scan requis' : rien a decrementer.
                    if (-not ($before -is [double] -or $before -is [int])) { continue }
                    # Jamais negatif : un profil grossi depuis le scan pourrait
                    # rendre le retrait superieur a la taille connue.
                    $script:ProfileSizes[$name] = [math]::Max(0.0, [math]::Round($before - $freedByProfile[$name], 1))
                    $adjusted++
                }
                if ($adjusted -gt 0) {
                    $script:ProfileSizesAdjustedAt = Get-Date
                    Save-ProfileSizeCache
                    Write-AgentLog "clean_downloads: cache tailles ajuste pour $adjusted profil(s)" -Level "INFO"
                }
            }
            catch { Write-AgentLog "clean_downloads: ajustement cache echoue: $_" -Level "WARN" }
        }
    }

    return @{
        dry_run        = $DryRun
        days           = $Days
        base           = $baseFull
        profiles_scanned = $profiles.Count
        folders_found  = $foldersSeen
        candidates     = $items.Count
        total_mb       = [math]::Round($totalBytes / 1MB, 1)
        deleted        = $deleted
        failed         = $failed
        freed_mb       = [math]::Round($freed, 1)
        top            = @($items | Sort-Object SizeMB -Descending | Select-Object -First 15 |
                           ForEach-Object { @{ profile = $_.Profile; name = $_.Name; size_mb = $_.SizeMB; age_days = $_.Age } })
        # Liste detaillee pour le modal de l'UI (v2.25.3) — plafonnee a 300
        # elements (tries par taille) : un resultat de commande n'est pas un
        # canal de masse, et 300 suffisent a decider. items_truncated dit
        # combien n'ont pas ete listes : la troncature s'annonce, jamais tacite.
        items          = @($items | Sort-Object SizeMB -Descending | Select-Object -First 300 |
                           ForEach-Object { @{ profile = $_.Profile; name = $_.Name; size_mb = $_.SizeMB; age_days = $_.Age; deleted = $_.Deleted } })
        items_truncated = [math]::Max(0, $items.Count - 300)
    }
}

function Get-UserProfiles {
    <#
    .SYNOPSIS
        Collecte les profils utilisateurs (nom + date), SANS calcul de taille.
    .DESCRIPTION
        🔴 La page « Espaces Profils » sert a repondre a UNE question : « le disque
        des profils est plein a 82 %, QUI consomme ? ». Elle affichait la volumetrie
        de R: (correcte) a cote de la liste de C:\Users du broker — soit 7 comptes
        d'ADMINISTRATION locaux totalisant 0 Mo, alors que R: contient 101 dossiers
        de VRAIS utilisateurs. Volumetrie et liste ne parlaient pas du meme disque :
        la page ne pouvait donc pas repondre a sa propre question (constate 31/07/2026).

        On enumere donc en PRIORITE le stockage de profils (R:), et on retombe sur
        C:\Users seulement s'il est absent. `Source` dit lequel a ete lu, pour que
        l'UI puisse etre honnete sur ce qu'elle montre.

        La TAILLE reste "Scan requis" : mesurer 101 arborescences a chaque heartbeat
        (10 s) ecroulerait l'hote. Le calcul se fait a la demande (analyze_profile).
    #>
    try {
        # R: = stockage des profils/redirections de la ferme (present sur le broker).
        $roots = @(
            @{ Path = 'R:\Redirections'; Source = 'R:\Redirections' },
            @{ Path = 'R:\';             Source = 'R:' },
            @{ Path = 'C:\Users';        Source = 'C:\Users' }
        )
        $exclude = @('Public','Default','Default User','All Users','Administrator')

        foreach ($root in $roots) {
            if (-not (Test-Path $root.Path)) { continue }
            $dirs = @(Get-ChildItem -Path $root.Path -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -notin $exclude -and
                               -not $_.Name.StartsWith('.') -and
                               -not $_.Name.StartsWith('Default') })
            if ($dirs.Count -eq 0) { continue }

            $profileList = @()
            foreach ($dir in $dirs) {
                try {
                    # Taille issue du dernier scan si disponible, sinon 'Scan requis'
                    $known = if ($script:ProfileSizes.ContainsKey($dir.Name)) { $script:ProfileSizes[$dir.Name] } else { 'Scan requis' }
                    $profileList += [PSCustomObject]@{
                        Name          = $dir.Name
                        LastWriteTime = $dir.LastWriteTime.ToString('yyyy-MM-dd HH:mm')
                        SizeMB        = $known
                        Source        = $root.Source
                        ScannedAt     = if ($script:ProfileSizesAt) { $script:ProfileSizesAt.ToString('yyyy-MM-dd HH:mm') } else { $null }
                        AdjustedAt    = if ($script:ProfileSizesAdjustedAt) { $script:ProfileSizesAdjustedAt.ToString('yyyy-MM-dd HH:mm') } else { $null }
                    }
                } catch { }
            }
            return @($profileList | Sort-Object LastWriteTime -Descending)
        }
        return @()
    }
    catch {
        Write-AgentLog "Erreur collecte profils: $_" -Level "ERROR"
        return @()
    }
}

function Get-ProfileFolderAnalysis {
    <#
    .SYNOPSIS
        Analyse détaillée de l'espace disque par dossier pour un profil
        Supporte les profils redirectionnés (lecteur réseau R:)
    #>
    param([string]$ProfileName)
    
    try {
        # Chercher le profil dans les emplacements possibles
        $profilePaths = @(
            "C:\Users\$ProfileName",      # Chemin local standard
            "R:\$ProfileName",              # Lecteur R: (redirections)
            "R:\Redirections\$ProfileName"  # Alternativement sous R:\Redirections
        )
        
        $basePath = $null
        foreach ($path in $profilePaths) {
            if (Test-Path $path) {
                $basePath = $path
                Write-AgentLog "Profil trouve a: $basePath" -Level "INFO"
                break
            }
        }
        
        if (-not $basePath) {
            return @{ error = "Profil introuvable" }
        }
        
        $analysis = @{
            total_mb = 0
            folders = @()
            profile_path = $basePath
        }
        
        # 1. Analyse spécifique pour AppData (pour avoir le détail Local/Roaming)
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $appDataFolders = @(
            @{name="AppData (Roaming)"; path="AppData\Roaming"},
            @{name="AppData (Local)"; path="AppData\Local"},
            @{name="AppData (LocalLow)"; path="AppData\LocalLow"}
        )
        
        foreach ($af in $appDataFolders) {
            $fp = Join-Path $basePath $af.path
            if (Test-Path $fp) {
                try {
                    $s = (Get-ChildItem -Path $fp -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum / 1MB
                    if ($s -ge 0.1) {
                        $analysis.folders += @{ name=$af.name; size_mb=[Math]::Round($s, 1) }
                        $analysis.total_mb += $s
                    }
                } catch {}
            }
        }

        # 2. Analyse dynamique de tous les dossiers racine (Capture Téléchargements, Bureau, etc.)
        try {
            $rootDirs = Get-ChildItem -Path $basePath -Directory -Force -ErrorAction SilentlyContinue
            foreach ($dir in $rootDirs) {
                if ($dir.Name -eq "AppData") { continue } # Déjà traité via AppDataFolders
                
                try {
                    $s = (Get-ChildItem -Path $dir.FullName -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum / 1MB
                    if ($s -ge 0.1) {
                        $analysis.folders += @{ name=$dir.Name; size_mb=[Math]::Round($s, 1) }
                        $analysis.total_mb += $s
                    }
                } catch {}
            }
        } catch {}

        # 3. Fichiers racine
        try {
            $rf = (Get-ChildItem -Path $basePath -File -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum / 1MB
            if ($rf -ge 0.1) {
                $analysis.folders += @{ name="(Fichiers Racine)"; size_mb=[Math]::Round($rf, 1) }
                $analysis.total_mb += $rf
            }
        } catch {}
        
        # Trier par taille décroissante
        $analysis.folders = @($analysis.folders | Sort-Object {$_.size_mb} -Descending)
        $analysis.total_mb = [Math]::Round($analysis.total_mb, 1)
        
        return $analysis
    }
    catch {
        Write-AgentLog "Erreur analyse profil: $_" -Level "ERROR"
        return @{ error = "Erreur lors de l'analyse" }
    }
}

function Get-Office365Status {
    <#
    .SYNOPSIS
        Etat rapide des licences/jetons Office 365 (Shared Computer Activation).
        Lecture seule. Utilise par le payload heartbeat (v2.9.0).
    #>
    $systemAccounts = @('Administrator','Administrateur','UvhdCleanupBin',
                        'defaultuser0','Public','Default','Default User','All Users',
                        'systemprofile','LocalService','NetworkService') +
                      @($IgnoredAccounts | Where-Object { $_ }) +
                  @("$env:SAM_IGNORED_ACCOUNTS" -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $staleDays   = 30
    $inactiveDays = 45
    $now = Get-Date

    # SCA (SharedComputerLicensing) au niveau serveur
    $c2rPath = 'HKLM:\SOFTWARE\Microsoft\Office\ClickToRun\Configuration'
    $cfg = Get-ItemProperty -Path $c2rPath -ErrorAction SilentlyContinue

    # Office absent de l'hote (pas de cle ClickToRun) : NE PAS scanner les profils.
    # Sinon chaque profil sans dossier Licensing est compte "jeton absent" et la page
    # Licences affiche des anomalies rouges sur un serveur qui n'a simplement pas
    # Office (observed on a lab host: 3 "missing" tokens reported for nothing).
    if (-not $cfg) {
        return @{
            sca_status     = 'not_installed'
            office_version = $null
            users_ok       = 0
            users_stale    = 0
            users_missing  = 0
            users          = @()
        }
    }

    $sca = $cfg.SharedComputerLicensing
    $scaStatus = switch ($sca) {
        '1'     { 'ok' }
        '0'     { 'disabled' }
        default { 'unknown' }
    }
    $officeVersion = $cfg.VersionToReport

    # Scan par profil
    $users = @()
    $usersDir = 'C:\Users'
    if (Test-Path $usersDir) {
        # ATTENTION au filtre TEMP : '^(...|TEMP\.)' n'excluait QUE les profils
        # temporaires suffixes (TEMP.DOMAINE) et laissait passer le dossier nomme
        # exactement 'TEMP' -> remonte comme un utilisateur en "jeton absent", avec
        # un bouton Reparer inutile (observed on a session host).
        # '^TEMP(\.|$)' couvre les deux formes sans exclure un vrai compte
        # (TEMPLIER.jean reste scanne).
        # Comptes MACHINE (se terminent par '$') : ce ne sont pas des utilisateurs.
        # `orchestrate_bootstrap` s'execute sous <BROKER>$ et cree donc un profil
        # C:\Users\<BROKER>$ sur chaque hote bootstrappe -> remonte en "jeton absent"
        # alors qu'un compte machine n'ouvrira jamais Office (constate 31/07/2026).
        $dirs = Get-ChildItem -Path $usersDir -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -notin $systemAccounts -and
                               $_.Name -notlike '*$' -and
                               $_.Name -notmatch '^(Default|Public)' -and
                               $_.Name -notmatch '^TEMP(\.|$)' -and
                               $_.Name -notmatch '\.\d{3}$' }
        foreach ($dir in $dirs) {
            try {
                # Profil inactif ?
                $lastWrite = $dir.LastWriteTime
                if (($now - $lastWrite).TotalDays -gt $inactiveDays) { continue }

                # Dossier jeton de licence SCA Office
                $tokenPath = Join-Path $dir.FullName 'AppData\Local\Microsoft\Office\16.0\Licensing'
                if (-not (Test-Path $tokenPath)) {
                    $users += @{ name = $dir.Name; status = 'missing'; age_days = $null; dn = (Get-UserDn $dir.Name) }
                    continue
                }
                $latest = Get-ChildItem -Path $tokenPath -File -ErrorAction SilentlyContinue |
                          Sort-Object LastWriteTime -Descending | Select-Object -First 1
                if (-not $latest) {
                    $users += @{ name = $dir.Name; status = 'missing'; age_days = $null; dn = (Get-UserDn $dir.Name) }
                    continue
                }
                $ageDays = [math]::Round(($now - $latest.LastWriteTime).TotalDays, 1)
                $status  = if ($ageDays -le $staleDays) { 'ok' } else { 'stale' }
                $users  += @{ name = $dir.Name; status = $status; age_days = $ageDays; dn = (Get-UserDn $dir.Name) }
            } catch { }
        }
    }

    # 🔴 Le @() est OBLIGATOIRE, pas cosmetique.
    # Quand le filtre ne ramene QU'UN SEUL utilisateur, le pipeline renvoie la
    # hashtable elle-meme et non un tableau : `.Count` compte alors ses CLES
    # (name/status/age_days = 3) au lieu des elements. Un hote avec 1 jeton absent
    # remontait donc "3 absents" — observed on two hosts (1 profile,
    # 3 reported; 1 TEMP, 3 reported), avec des totaux de ferme faux
    # en consequence. @() force le tableau, donc un vrai comptage.
    $ok      = @($users | Where-Object { $_.status -eq 'ok' }).Count
    $stale   = @($users | Where-Object { $_.status -eq 'stale' }).Count
    $missing = @($users | Where-Object { $_.status -eq 'missing' }).Count

    Save-UserDnCache

    return @{
        sca_status     = $scaStatus
        office_version = $officeVersion
        users_ok       = $ok
        users_stale    = $stale
        users_missing  = $missing
        users          = @($users)
    }
}

# --- Resolution AD du DN d'un utilisateur (v2.29.0) ---------------------------
# L'agent tourne en SYSTEM sur une machine du DOMAINE : il interroge l'AD dans le
# contexte du compte machine, donc SANS AUCUN IDENTIFIANT. C'est ce qui permet de
# ne PAS diffuser le compte de bind (userad, admin du domaine) dans le .env d'une
# application de plus.
#
# ➡️ L'agent remonte le DN BRUT et ne decide RIEN : c'est le serveur qui applique
#    le perimetre. Coder l'OU ici obligerait a une mise a jour de flotte a chaque
#    changement de perimetre — ce qu'on ne veut jamais avoir a faire pour un filtre.
$script:UserDnCache     = @{}
$script:UserDnCacheFile = Join-Path $env:ProgramData 'SysAdminMaster\user-dn.json'

function Import-UserDnCache {
    # Une resolution AD coute un aller-retour reseau : la garder en RAM seule la
    # ferait refaire a chaque redemarrage de l'agent (piege 13).
    try {
        if (Test-Path $script:UserDnCacheFile) {
            $raw = Get-Content $script:UserDnCacheFile -Raw -ErrorAction Stop | ConvertFrom-Json
            foreach ($p in $raw.PSObject.Properties) { $script:UserDnCache[$p.Name] = $p.Value }
        }
    } catch { $script:UserDnCache = @{} }
}

function Save-UserDnCache {
    try {
        $dir = Split-Path $script:UserDnCacheFile -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $script:UserDnCache | ConvertTo-Json -Depth 3 | Set-Content $script:UserDnCacheFile -Encoding UTF8
    } catch { }
}

function Get-UserDn {
    <#
    .SYNOPSIS
        distinguishedName AD d'un compte, ou $null s'il est inconnu du domaine
        (comptes locaux, comptes de service). Resultat mis en cache.
    .NOTES
        Un $null est une INFORMATION, pas un echec : "inconnu de l'AD" est
        precisement ce qui distingue un compte local d'un collaborateur.
        On memorise donc aussi les $null, sinon chaque heartbeat rejouerait la
        recherche pour les memes comptes absents.
    #>
    param([string]$UserName)
    if ($script:UserDnCache.ContainsKey($UserName)) { return $script:UserDnCache[$UserName] }
    $dn = $null
    try {
        $searcher = New-Object System.DirectoryServices.DirectorySearcher
        $searcher.Filter = "(&(objectCategory=person)(objectClass=user)(sAMAccountName=$UserName))"
        $searcher.PageSize = 1
        [void]$searcher.PropertiesToLoad.Add('distinguishedName')
        $found = $searcher.FindOne()
        if ($found) { $dn = [string]$found.Properties['distinguishedname'][0] }
    } catch { $dn = $null }
    $script:UserDnCache[$UserName] = $dn
    return $dn
}

function Resolve-UserSid {
    <#
    .SYNOPSIS
        SID d'un compte, ou $null. Jamais de nom traduit dans un test (piege 10).
    #>
    param([string]$UserName)
    try {
        return (New-Object System.Security.Principal.NTAccount($UserName)).Translate(
                   [System.Security.Principal.SecurityIdentifier]).Value
    } catch { return $null }
}

function Assert-OfficeRepairReady {
    <#
    .SYNOPSIS
        Prerequis COMMUNS aux deux reparations Office. Renvoie le contexte ou throw.

    .DESCRIPTION
        🔴 Ces prerequis sont l'INVERSE de ceux d'avant la v2.28.0, et c'est le
        coeur du correctif. L'ancienne version exigeait une session FERMEE, en
        s'appuyant sur l'idee qu'Office verrouillerait les fichiers autrement.
        Mais les profils de cette ferme sont en UPD : C:\Users\<user> n'existe
        QUE pendant la session. Garde-fou et prerequis se contredisaient, il
        n'existait aucune fenetre executable, et l'action n'a jamais pu reparer
        quoi que ce soit (constate le 31/08/2026 : les deux echecs obtenus coup
        sur coup sur le meme utilisateur).

        La bonne condition, prouvee a la main le meme soir :
          session OUVERTE (profil monte + ruche HKU chargee) et Office FERME.
        C'est Office qui verrouille, pas la session.
    #>
    param([string]$UserName)

    if (-not $UserName) { throw "Parametre 'user' manquant" }

    $userDir = Join-Path 'C:\Users' $UserName
    if (-not (Test-Path $userDir)) {
        throw ("Profil non monte pour $UserName. Les profils de cette ferme sont en UPD : " +
               "C:\Users\$UserName n'existe que pendant sa session. Demander a l'utilisateur " +
               "d'OUVRIR sa session puis de FERMER Office, et relancer.")
    }

    $sid = Resolve-UserSid -UserName $UserName
    if (-not $sid) { throw "SID introuvable pour $UserName (compte inconnu du domaine ?)" }

    # La ruche chargee n'est PAS un prerequis : c'est le choix de strategie.
    #   - chargee   = session ouverte  -> on ecrit dans HKEY_USERS (cas UPD, le courant) ;
    #   - absente   = profil persistant hors session -> ruche hors ligne (reg load).
    # Exiger l'un OU l'autre exclurait la moitie des cas : les profils UPD
    # n'existent que session ouverte, les comptes d'administration et de service
    # persistent au contraire sur le disque et se reparent hors session.
    $hiveLoaded = Test-Path "Registry::HKEY_USERS\$sid"


    # Office doit etre ferme : il verrouille les fichiers de licence et reecrit
    # son cache d'identite en sortie. On teste par PROPRIETAIRE du process, pas
    # par presence globale : sur un RDS, l'Outlook d'un collegue ne doit pas
    # bloquer la reparation d'un autre.
    $officeProcs = @('OUTLOOK','WINWORD','EXCEL','POWERPNT','MSACCESS','ONENOTE','MSPUB')
    $running = @()
    foreach ($procName in $officeProcs) {
        try {
            $procs = @(Get-Process -Name $procName -IncludeUserName -ErrorAction SilentlyContinue |
                       Where-Object { $_.UserName -and (($_.UserName -split '\\')[-1]) -ieq $UserName })
            if ($procs.Count -gt 0) { $running += ("{0} x{1}" -f $procName, $procs.Count) }
        } catch { }
    }
    if ($running.Count -gt 0) {
        throw ("Office est ouvert pour $UserName (" + ($running -join ', ') + "). Demander la " +
               "fermeture complete d'Office (sa session reste ouverte), puis relancer.")
    }

    return @{ user = $UserName; sid = $sid; userDir = $userDir; hiveLoaded = $hiveLoaded }
}

function Remove-OldOfficeBackups {
    <#
    .SYNOPSIS
        Retention 30 j des sauvegardes de reparation Office (jeton et identite).
        Sans purge, ces dossiers grossissent indefiniment.
    #>
    foreach ($root in @('C:\Temp\o365-token-backup', 'C:\Temp\o365-identity-backup')) {
        if (Test-Path $root) {
            Get-ChildItem -Path $root -ErrorAction SilentlyContinue |
                Where-Object { ((Get-Date) - $_.LastWriteTime).TotalDays -gt 30 } |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-OfficeLicenseRenewal {
    <#
    .SYNOPSIS
        Panne 1/2 : jeton SCA perime (> 30 j). Purge le dossier Licensing.
        Ne touche NI le cache d'identite NI la session.
    #>
    param($Ctx)

    $tokenPath = Join-Path $Ctx.userDir 'AppData\Local\Microsoft\Office\16.0\Licensing'
    if (-not (Test-Path $tokenPath)) {
        return "Aucun jeton a renouveler pour $($Ctx.user) : dossier Licensing absent. L'utilisateur doit ouvrir Office pour en obtenir un (une purge n'y peut rien)."
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupDir = "C:\Temp\o365-token-backup\$($env:COMPUTERNAME)-$stamp-$($Ctx.user)"
    Copy-Item -Path $tokenPath -Destination $backupDir -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $backupDir)) {
        throw "Sauvegarde du jeton impossible pour $($Ctx.user) - rien n'a ete supprime."
    }

    Remove-Item -Path "$tokenPath\*" -Force -Recurse -ErrorAction SilentlyContinue

    # 🔴 VERIFICATION POST-PURGE (obligatoire, piege 11).
    # -ErrorAction SilentlyContinue avale les echecs de suppression : sans ce
    # controle l'agent repondait "Jeton purge" alors que RIEN n'avait bouge.
    $remaining = @(Get-ChildItem -Path $tokenPath -Recurse -File -Force -ErrorAction SilentlyContinue)
    if ($remaining.Count -gt 0) {
        $sample = ($remaining | Select-Object -First 3 | ForEach-Object { $_.Name }) -join ', '
        throw "Purge INCOMPLETE pour $($Ctx.user) : $($remaining.Count) fichier(s) encore present(s) - $sample. Office est-il vraiment ferme ? Sauvegarde: $backupDir"
    }
    return "Jeton de licence purge et suppression VERIFIEE pour $($Ctx.user). Sauvegarde: $backupDir. L'utilisateur doit rouvrir Office."
}

function Invoke-OfficeIdentityRepair {
    <#
    .SYNOPSIS
        Panne 2/2 : cache d'identite corrompu (code 0x80070002 a l'authentification,
        alors que le jeton est valide). Purge la cle Identity - dans la ruche
        CHARGEE si la session est ouverte, sinon dans la ruche HORS LIGNE - et
        renomme IdentityCache. NE TOUCHE PAS A LA LICENCE.

    .NOTES
        Sequence validee en conditions reelles le 31/08/2026 : Outlook a rouvert
        SANS demander de reauthentification (Office reconstruit son cache depuis
        la session Windows deja authentifiee) - preuve que seul ce cache etait
        en cause. Export du registre avant suppression, et RENOMMAGE du dossier
        plutot que suppression : entierement reversible.
    #>
    param($Ctx)

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $done = @()

    # Strategie selon que la ruche de l'utilisateur est chargee ou non.
    $hiveName = $null
    if ($Ctx.hiveLoaded) {
        $keyNative = "HKU\$($Ctx.sid)\Software\Microsoft\Office\16.0\Common\Identity"
        $keyPath   = "Registry::HKEY_USERS\$($Ctx.sid)\Software\Microsoft\Office\16.0\Common\Identity"
    } else {
        $hiveFile = Join-Path $Ctx.userDir 'NTUSER.DAT'
        if (-not (Test-Path $hiveFile)) {
            throw "Ni ruche chargee ni NTUSER.DAT pour $($Ctx.user) - identite non reparable."
        }
        $hiveName  = "SAMHive_" + ($Ctx.user -replace '[^A-Za-z0-9]','')
        & reg.exe load "HKLM\$hiveName" $hiveFile 2>&1 | Out-Null
        if (-not (Test-Path "Registry::HKEY_LOCAL_MACHINE\$hiveName")) {
            throw "Chargement de la ruche hors ligne impossible pour $($Ctx.user) (fichier verrouille ?)"
        }
        $keyNative = "HKLM\$hiveName\Software\Microsoft\Office\16.0\Common\Identity"
        $keyPath   = "Registry::HKEY_LOCAL_MACHINE\$hiveName\Software\Microsoft\Office\16.0\Common\Identity"
    }

    try {
        if (Test-Path $keyPath) {
            $backupRoot = 'C:\Temp\o365-identity-backup'
            if (-not (Test-Path $backupRoot)) { New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null }
            $regFile = Join-Path $backupRoot "$($env:COMPUTERNAME)-$stamp-$($Ctx.user).reg"
            & reg.exe export $keyNative $regFile /y 2>&1 | Out-Null
            if (-not (Test-Path $regFile)) {
                throw "Export du registre echoue pour $($Ctx.user) - rien n'a ete supprime."
            }
            & reg.exe delete $keyNative /f 2>&1 | Out-Null
            if (Test-Path $keyPath) {
                throw "Cle Identity toujours presente apres suppression pour $($Ctx.user). Sauvegarde: $regFile"
            }
            $done += "cle Identity supprimee (sauvegarde $regFile)"
        }
    }
    finally {
        # Une ruche chargee et jamais dechargee verrouille NTUSER.DAT et empeche
        # l'utilisateur d'ouvrir sa session. Le finally n'est pas optionnel.
        if ($hiveName) {
            [gc]::Collect()
            Start-Sleep -Milliseconds 500
            & reg.exe unload "HKLM\$hiveName" 2>&1 | Out-Null
        }
    }

    $cachePath = Join-Path $Ctx.userDir 'AppData\Local\Microsoft\IdentityCache'
    if (Test-Path $cachePath) {
        Rename-Item -Path $cachePath -NewName "IdentityCache.bak-$stamp" -Force -ErrorAction SilentlyContinue
        if (Test-Path $cachePath) {
            throw "IdentityCache toujours present apres renommage pour $($Ctx.user) - dossier verrouille (Office encore ouvert ?)."
        }
        $done += "IdentityCache renomme en IdentityCache.bak-$stamp"
    }

    if ($done.Count -eq 0) {
        return "Rien a reparer pour $($Ctx.user) : ni cle Identity ni dossier IdentityCache presents."
    }
    return "Identite Office reparee et VERIFIEE pour $($Ctx.user) : " + ($done -join ' ; ') + ". La licence n'a PAS ete touchee. L'utilisateur peut rouvrir Office."
}

function Get-OfficeAuthDiagnostic {
    <#
    .SYNOPSIS
        Panne 3/3, volet DIAGNOSTIC : lit le journal Azure AD de l'hote et rend
        le code d'erreur REEL de l'authentification, pour l'utilisateur vise.

    .DESCRIPTION
        Raison d'etre (post-mortem, one user on one session host) : l'outil ne
        savait nommer que deux causes - jeton perime, cache d'identite. Les deux
        gestes ont ete passes a l'aveugle, tous deux "reussis", sans effet : la
        vraie cause etait AADSTS50196 (client request loop) cote Azure AD, et
        elle n'etait lisible QUE dans Microsoft-Windows-AAD/Operational.

        LECTURE SEULE. Ne modifie rien, ne necessite pas Office ferme.

    .NOTES
        🔴 Deux defauts du premier jet, corriges en 2.31.1 le jour meme - tous
        deux produisaient un "0 evenement" qui se lisait "rien a signaler" :

        1. Le filtre portait sur le MESSAGE (nom ou SID). Or sur ce journal
           AUCUN evenement ne cite le nom de l'utilisateur : l'identite est
           portee par le champ UserId de l'enregistrement. Mesure du 23/09 :
           614 evenements sur 6 h, 0 citant le nom du compte, 69 portant son
           SID en UserId. Le filtre est donc pose COTE SOURCE (FilterHashtable
           UserID) - plus juste ET moins couteux que lire puis scanner.
        2. MaxEvents tronque aux evenements les PLUS RECENTS : avec un journal
           a ~100 evts/h, la fenetre demandee n'etait pas honoree et les traces
           recherchees etaient deja sorties. On ANNONCE desormais la troncature
           au lieu de conclure sur une mesure amputee.
    #>
    param([string]$UserName, [int]$Hours = 6)

    if (-not $UserName) { throw "Parametre 'user' manquant" }
    $sid = Resolve-UserSid -UserName $UserName
    $since = (Get-Date).AddHours(-[Math]::Abs($Hours))
    $log = 'Microsoft-Windows-AAD/Operational'
    $cap = 400
    $lines = New-Object System.Collections.ArrayList
    $codes = New-Object System.Collections.ArrayList
    $corr  = New-Object System.Collections.ArrayList
    $mine = @()
    $mode = ''
    $truncated = $false

    if (-not $sid) { $null = $lines.Add("ATTENTION : SID introuvable pour $UserName - filtrage par message, moins fiable.") }

    # Voie 1 : filtrage COTE SOURCE par UserID. C'est la seule qui trouve
    # quelque chose sur ce journal, et elle ne rapatrie que le pertinent.
    if ($sid) {
        try {
            $mine = @(Get-WinEvent -FilterHashtable @{LogName=$log; StartTime=$since; UserID=$sid} -MaxEvents $cap -ErrorAction Stop)
            $mode = "filtre UserID (cote source)"
        } catch {
            if ($_.Exception.Message -match 'No events were found|Aucun') {
                $mine = @(); $mode = "filtre UserID (cote source)"
            } else {
                $mode = ''
            }
        }
    }

    # Voie 2 (repli) : lecture puis scan. Conserve pour un hote ou le filtre
    # UserID n'est pas servi - mais on DIT qu'on est en repli, et on dit si la
    # lecture a ete tronquee, sinon un 0 se lirait comme un feu vert.
    if (-not $mode) {
        try {
            $ev = @(Get-WinEvent -FilterHashtable @{LogName=$log; StartTime=$since} -MaxEvents $cap -ErrorAction Stop)
        } catch {
            $null = $lines.Add("journal $log indisponible : $($_.Exception.Message)")
            $null = $lines.Add("VERDICT: NON MESURE - le journal n'a pas pu etre lu.")
            return ($lines -join [Environment]::NewLine)
        }
        if ($ev.Count -ge $cap) { $truncated = $true }
        $mine = @($ev | Where-Object {
            ($sid -and $_.UserId -and $_.UserId.Value -eq $sid) -or
            ($_.Message -and $_.Message -match [regex]::Escape($UserName))
        })
        $mode = "repli : lecture de $($ev.Count) evenements puis scan"
    }

    if ($mine.Count -ge $cap) { $truncated = $true }
    $null = $lines.Add("$log sur ${Hours}h - $mode : $($mine.Count) evenement(s) pour $UserName")
    if ($mine.Count -gt 0) {
        $null = $lines.Add("  du " + $mine[-1].TimeCreated + " au " + $mine[0].TimeCreated)
    }

    foreach ($e in $mine) {
        foreach ($m in @([regex]::Matches($e.Message, 'AADSTS\d+'))) {
            if (-not $codes.Contains($m.Value)) { $null = $codes.Add($m.Value) }
        }
        foreach ($m in @([regex]::Matches($e.Message, '0x[0-9A-Fa-f]{8}'))) {
            if (-not $codes.Contains($m.Value)) { $null = $codes.Add($m.Value) }
        }
        foreach ($m in @([regex]::Matches($e.Message, 'Correlation ID: ([0-9a-f\-]{36})'))) {
            $v = $m.Groups[1].Value
            if (-not $corr.Contains($v)) { $null = $corr.Add($v) }
        }
    }

    foreach ($e in @($mine | Select-Object -First 3)) {
        $msg = ($e.Message -replace '\s+', ' ')
        if ($msg.Length -gt 240) { $msg = $msg.Substring(0, 240) }
        $null = $lines.Add("  [$($e.TimeCreated)] id=$($e.Id) $msg")
    }

    # 🔴 LIRE L'EFFET AVANT DE JUGER SUR LES CODES (corrige en 2.31.2).
    # Un journal d'erreurs d'authentification est du BRUIT NORMAL sur une ferme
    # MFA : chaque renouvellement de jeton passe par un refus silencieux puis une
    # interaction. Mesure du 23/09 sur 3 hotes : michele.avril (jeton 2 j),
    # theo.konieczny (5,9 j), clara.bolvy (21,7 j) ont tous produit AADSTS50076
    # en allant parfaitement bien ; lauriane.seaman a produit la BOUCLE
    # AADSTS50196 et a obtenu son jeton seule (9 j -> 0 j) sans intervention.
    # Le seul fait qui tranche est donc : Office a-t-il fini par avoir sa licence ?
    $licPath = Join-Path (Join-Path 'C:\Users' $UserName) 'AppData\Local\Microsoft\Office\16.0\Licensing'
    $licFiles = @()
    $licKnown = $false
    if (Test-Path -LiteralPath (Join-Path 'C:\Users' $UserName)) {
        $licKnown = $true
        if (Test-Path -LiteralPath $licPath) {
            $licFiles = @(Get-ChildItem -LiteralPath $licPath -File -Recurse -Force -ErrorAction SilentlyContinue)
        }
    }
    if ($licKnown) {
        if ($licFiles.Count -gt 0) {
            $licAge = [math]::Round(((Get-Date) - (($licFiles | Sort-Object LastWriteTime -Descending)[0].LastWriteTime)).TotalDays, 1)
            $null = $lines.Add("LICENCE: presente, jeton age de $licAge j")
        } else {
            $null = $lines.Add("LICENCE: ABSENTE (dossier Licensing vide) - Office n'a PAS obtenu de jeton")
        }
    } else {
        $null = $lines.Add("LICENCE: non mesurable (profil non monte)")
    }

    if ($codes.Count -gt 0) { $null = $lines.Add("CODES: " + ($codes -join ', ')) }
    if ($corr.Count -gt 0)  { $null = $lines.Add("CORRELATION IDS: " + (($corr | Select-Object -First 5) -join ', ')) }

    # Verdict. Deliberement conservateur : un code non reconnu est rendu tel
    # quel, jamais interprete au hasard - et une mesure tronquee ou vide ne
    # rend JAMAIS un verdict rassurant (un vide se lit "rien a signaler").
    $mfa  = ($codes -contains 'AADSTS50076' -or $codes -contains 'AADSTS50079' -or $codes -contains 'AADSTS50158')
    $loop = ($codes -contains 'AADSTS50196')
    $licOk = ($licKnown -and $licFiles.Count -gt 0)

    if ($mine.Count -eq 0) {
        $verdict = "AUCUNE TRACE pour cet utilisateur sur la fenetre - ce n'est PAS un feu vert : elargir avec hours, ou l'incident est anterieur."
    } elseif ($licOk) {
        # Des erreurs AVEC une licence obtenue = le mecanisme normal a fonctionne.
        $verdict = "RAS - Office a bien sa licence. Les codes ci-dessus sont le BRUIT NORMAL du renouvellement sur une ferme MFA (refus silencieux, puis interaction). Ne PAS reparer sur la foi de ces codes : chercher ailleurs si l'utilisateur se plaint."
    } elseif ($loop -and $mfa) {
        $verdict = "BLOQUE : boucle AADSTS50196 + MFA (AADSTS50076) SANS licence obtenue. Le broker reboucle au lieu de laisser la MFA se presenter. Geste : repair_office_broker, PUIS fermeture/reouverture de session (le PRT ne se renegocie qu'a l'ouverture), PUIS connexion INTERACTIVE (portail ou Fichier > Compte) - une requete d'arriere-plan ne peut pas servir une MFA."
    } elseif ($loop) {
        $verdict = "BOUCLE AADSTS50196 sans licence obtenue. A noter : cette boucle se resout souvent SEULE (cas observe le 23/09 : jeton 9 j -> 0 j sans intervention). Attendre un cycle avant d'agir ; si le 48v35 persiste, repair_office_broker."
    } elseif ($mfa) {
        $verdict = "MFA demandee (AADSTS50076) et licence pas encore obtenue. C'est l'etat NORMAL entre la demande et l'interaction : faire ouvrir une connexion INTERACTIVE (portail Office ou Fichier > Compte). Ce code seul n'est PAS une anomalie."
    } elseif ($codes -contains 'AADSTS53003') {
        $verdict = "ACCES BLOQUE par une strategie d acces conditionnel du tenant - hors perimetre de l hote."
    } elseif ($codes -contains 'AADSTS700016' -or $codes -contains 'AADSTS90002') {
        $verdict = "Application ou tenant introuvable - a remonter au tenant."
    } elseif ($codes -contains '0x80070002') {
        $verdict = "Cache d identite Office (0x80070002) sans licence obtenue - repair_office_identity est le geste adapte."
    } else {
        $verdict = "indetermine - des evenements existent mais aucun code connu."
    }
    if ($truncated) {
        $verdict = "[MESURE TRONQUEE a $cap evenements - le verdict porte sur une partie seulement] " + $verdict
    }
    $null = $lines.Add("VERDICT: $verdict")
    return ($lines -join [Environment]::NewLine)
}

function Invoke-OfficeBrokerReset {
    <#
    .SYNOPSIS
        Panne 3/3 : les caches de jetons du BROKER Azure AD bouclent.
        Symptome : tag Office 48v35 alors que jeton ET identite ont deja ete
        purges, avec AADSTS50196 dans Microsoft-Windows-AAD/Operational.

    .DESCRIPTION
        Ce que les deux autres gestes NE touchent PAS, et qui manquait :
          - %LOCALAPPDATA%\Microsoft\OneAuth            (magasin d auth moderne)
          - ...\Packages\Microsoft.AAD.BrokerPlugin_*\AC\TokenBroker\Accounts

        Le package AppX lui-meme n est JAMAIS touche : renommer le dossier du
        package casserait son enregistrement pour l utilisateur. On ne vise que
        les caches, que Windows et Office reconstruisent seuls.

        Strictement REVERSIBLE : renommage horodate, jamais de suppression.
        Effet VERIFIE apres coup (piege 11 : -ErrorAction SilentlyContinue
        rend un succes menteur).

    .NOTES
        Ce geste agit sur le PROFIL. Si la boucle persiste apres reouverture
        d Office, la cause est cote tenant (strategie, licence, compte) et
        aucune purge locale n y peut quoi que ce soit : remonter les
        Correlation ID rendus par Get-OfficeAuthDiagnostic.
    #>
    param($Ctx)

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $done = @()
    $skipped = @()

    $targets = New-Object System.Collections.ArrayList
    $null = $targets.Add((Join-Path $Ctx.userDir 'AppData\Local\Microsoft\OneAuth'))
    $pkgRoot = Join-Path $Ctx.userDir 'AppData\Local\Packages'
    if (Test-Path $pkgRoot) {
        $brokers = @(Get-ChildItem -LiteralPath $pkgRoot -Directory -Force -ErrorAction SilentlyContinue |
                     Where-Object { $_.Name -like 'Microsoft.AAD.BrokerPlugin_*' })
        foreach ($b in $brokers) {
            $null = $targets.Add((Join-Path $b.FullName 'AC\TokenBroker\Accounts'))
        }
    }

    foreach ($t in $targets) {
        if (-not (Test-Path -LiteralPath $t)) { $skipped += (Split-Path $t -Leaf); continue }
        $leaf = Split-Path $t -Leaf
        $newName = "$leaf.bak-$stamp"
        try {
            Rename-Item -LiteralPath $t -NewName $newName -Force -ErrorAction Stop
        } catch {
            throw ("Renommage impossible pour $($Ctx.user) : $t est verrouille ($($_.Exception.Message)). " +
                   "Office est-il bien ferme ? Aucun autre element n a ete modifie.")
        }
        # Verification obligatoire : le chemin d origine doit avoir disparu.
        if (Test-Path -LiteralPath $t) {
            throw "Renommage annonce mais $t est toujours present pour $($Ctx.user)."
        }
        $done += "$leaf renomme en $newName"
    }

    if ($done.Count -eq 0) {
        return ("Rien a reinitialiser pour $($Ctx.user) : aucun cache de broker present " +
                "(" + (@($skipped) -join ', ') + "). Si l erreur 48v35 persiste, la cause n est pas locale.")
    }
    return ("Broker Azure AD reinitialise et VERIFIE pour $($Ctx.user) : " + ($done -join ' ; ') +
            ". Sauvegardes par renommage dans le profil, la licence n a PAS ete touchee. " +
            "L utilisateur doit FERMER puis ROUVRIR sa session, puis relancer Office.")
}

function Get-SystemMetrics {
    <#
    .SYNOPSIS
        Collecte toutes les metriques systeme en une seule passe
    #>
    try {
        # Etat "occupe" (v2.26.0) — calcule hors du litteral de hashtable.
        $busyInfo = $null
        if ($script:BusyRemaining -gt 0) {
            $busyInfo = @{ remaining = $script:BusyRemaining; current = "$script:BusyCurrent" }
        }

        $os = Get-CimInstance Win32_OperatingSystem
        $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
        $uptime = (Get-Date) - $os.LastBootUpTime
        
        # Disques
        $disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
            @{
                Drive = $_.DeviceID
                UsedPercent = [math]::Round(($_.Size - $_.FreeSpace) / $_.Size * 100, 1)
                FreeGB = [math]::Round($_.FreeSpace / 1GB, 1)
                TotalGB = [math]::Round($_.Size / 1GB, 1)
            }
        }
        
        # Top 5 processus CPU (Correction v2.7.5 : Normalisation par rapport au nombre de coeurs)
        $numCpus = [int]$env:NUMBER_OF_PROCESSORS
        if ($numCpus -eq 0) { $numCpus = 1 }

        # Requete perf capturee une seule fois : sert au top 5 ET a la charge
        # par utilisateur (v2.25.0) - pas de double lecture WMI par heartbeat.
        $perfProcs = @(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process)
        $topProcesses = $perfProcs | 
            Where-Object { $_.Name -notmatch 'Idle|_Total|wmiprvse' } | 
            Sort-Object PercentProcessorTime -Descending | Select-Object -First 5 | ForEach-Object {
                $rawCpu = $_.PercentProcessorTime
                # Normalisation : Si la valeur brute depasse 100 * Coeurs, c'est une lecture erronee de WMI
                # On cap la valeur pour eviter les "ghost percentages"
                $normCpu = [math]::Round($rawCpu / $numCpus, 1)
                if ($normCpu -gt 100) { $normCpu = 100.0 }

                @{
                    Name = $_.Name
                    CPU = $normCpu
                    MemMB = [math]::Round($_.WorkingSetPrivate / 1MB, 1)
                }
            }
        
        # Sessions RDS locales (v2.7.6 - Collecte detaillee)
        $sessions = @{ Active = 0; Disconnected = 0; Total = 0; List = @() }
        try {
            # query user retourne: USERNAME, SESSIONNAME, ID, STATE, IDLE TIME, LOGON TIME
            $queryResult = query user 2>$null
            if ($queryResult) {
                $lines = $queryResult | Select-Object -Skip 1
                foreach ($line in $lines) {
                    $l = $line.Trim() -replace '^>', ''
                    $parts = $l -split '\s+'
                    if ($parts.Count -ge 4) {
                        $user = $parts[0]
                        $id = $null
                        $state = $null
                        $idle = $null
                        
                        # Si le 2eme element est purement numerique, c'est l'ID (SessionName manquant)
                        if ($parts[1] -match '^\d+$') {
                            $id = $parts[1]
                            $state = $parts[2]
                            $idle = $parts[3]
                        } else {
                            $id = $parts[2]
                            $state = $parts[3]
                            $idle = $parts[4]
                        }

                        # Normalisation des etats (Support FR/EN)
                        if ($state -match "^Act") { $state = "Active" }
                        else { $state = "Disconnected" }
                        
                        $s = @{
                            UserName = $user
                            SessionId = $id
                            SessionState = $state
                            IdleTime = $idle
                            HostServer = $script:Hostname
                        }
                        $sessions.List += $s
                        if ($state -eq "Active") { $sessions.Active++ } else { $sessions.Disconnected++ }
                        $sessions.Total++
                    }
                }
            }
        } catch { }
        
        # --- Charge par utilisateur (v2.25.0, robuste 2.25.1) ---
        # Agregation des process par proprietaire de SESSION : RAM (WorkingSet),
        # nb de process et top 3 par memoire viennent de Get-Process (toujours
        # disponible, l'agent est SYSTEM). Le CPU vient de la requete perf deja
        # capturee — mais cette classe WMI peut etre CORROMPUE sur un hote
        # (one host: 0x80041010 "Classe non valide") : le CPU est
        # alors null et l'UI affiche "-", jamais un 0 invente. On ne garde que
        # les proprietaires presents dans la liste de sessions (query user) :
        # filtre locale-proof, aucun libelle de compte systeme traduit (piege 10).
        $userLoad = @()
        try {
            $sessionUsers = @{}
            foreach ($s in $sessions.List) { $sessionUsers[$s.UserName.ToLower()] = $true }
            if ($sessionUsers.Count -gt 0) {
                $cpuByPid = @{}
                foreach ($pp in $perfProcs) { $cpuByPid[[int]$pp.IDProcess] = $pp.PercentProcessorTime }
                $havePerf = ($perfProcs.Count -gt 0)
                $agg = @{}
                Get-Process -IncludeUserName -ErrorAction SilentlyContinue | ForEach-Object {
                    if ($_.UserName) {
                        $leaf = ($_.UserName -split '\\')[-1]
                        $k = $leaf.ToLower()
                        if ($sessionUsers.ContainsKey($k)) {
                            if (-not $agg.ContainsKey($k)) {
                                $agg[$k] = @{ user = $leaf; cpu = 0.0; mem_mb = 0; procs = 0; top = @{} }
                            }
                            $a = $agg[$k]
                            $memMB = [math]::Round($_.WorkingSet64 / 1MB, 0)
                            $a.mem_mb += $memMB
                            $a.procs  += 1
                            $procCpu = 0.0
                            if ($cpuByPid.ContainsKey([int]$_.Id)) {
                                $procCpu = ($cpuByPid[[int]$_.Id] / $numCpus)
                                $a.cpu += $procCpu
                            }
                            # Detail par NOM de process (un nom = souvent plusieurs PID)
                            if (-not $a.top.ContainsKey($_.ProcessName)) {
                                $a.top[$_.ProcessName] = @{ mem = 0; cpu = 0.0; n = 0 }
                            }
                            $t = $a.top[$_.ProcessName]
                            $t.mem += $memMB
                            $t.cpu += $procCpu
                            $t.n   += 1
                        }
                    }
                }
                $userLoad = @($agg.Values | ForEach-Object {
                    $cpuVal = $null
                    if ($havePerf) {
                        $cpuVal = [math]::Round($_.cpu, 1)
                        if ($cpuVal -gt 100) { $cpuVal = 100.0 }
                    }
                    $useCpu = $havePerf
                    $topList = @($_.top.GetEnumerator() | Sort-Object { $_.Value.mem } -Descending |
                                 Select-Object -First 5 | ForEach-Object {
                                     @{ name = $_.Key
                                        mem_mb = $_.Value.mem
                                        cpu = if ($useCpu) { [math]::Round($_.Value.cpu, 1) } else { $null }
                                        n = $_.Value.n }
                                 })
                    @{ user = $_.user; cpu = $cpuVal; mem_mb = $_.mem_mb; procs = $_.procs; top = $topList }
                })
            }
        } catch {
            Write-AgentLog "user_load: collecte impossible: $_" -Level "WARN"
        }

        # Services RDS critiques
        $rdsServices = @("TermService", "SessionEnv", "UmRdpService") | ForEach-Object {
            $svc = Get-Service -Name $_ -ErrorAction SilentlyContinue
            if ($svc) {
                @{ Name = $_; Status = $svc.Status.ToString() }
            }
        }

        # Collecte des profils utilisateurs (nouveau v2.7.5)
        $profiles = Get-UserProfiles

        # Etat licences/jetons Office 365 (v2.9.0)
        $o365 = Get-Office365Status

        # Construire le payload
        return @{
            hostname = $script:Hostname
            timestamp = (Get-Date -Format "o")
            agent_version = $script:AgentVersion
            os = @{
                Caption = $os.Caption
                Version = $os.Version
                LastBoot = $os.LastBootUpTime.ToString()
            }
            cpu = [math]::Round($cpu, 1)
            ram = @{
                UsedPercent = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / $os.TotalVisibleMemorySize * 100, 1)
                FreeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
                TotalGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
                # Le COMMIT (memoire validee) est une grandeur DISTINCTE de la RAM : c'est lui
                # qui borne la creation de tout processus/session neuf. Un broker est reste
                # 40 h sans pouvoir ouvrir une session avec UsedPercent a
                # 56,5 % -- rassurant -- pendant que le commit etait sature par une fuite
                # rdpclip. Une RAM saine ne dit RIEN du commit. Cf. piege 36 du skill.
                CommitLimitGB = [math]::Round($os.TotalVirtualMemorySize / 1MB, 2)
                CommitFreeGB = [math]::Round($os.FreeVirtualMemory / 1MB, 2)
                CommitPercent = [math]::Round(($os.TotalVirtualMemorySize - $os.FreeVirtualMemory) / $os.TotalVirtualMemorySize * 100, 1)
            }
            uptime = "{0}d {1}h {2}m" -f $uptime.Days, $uptime.Hours, $uptime.Minutes
            disks = @($disks)
            processes_top5 = @($topProcesses)
            sessions = $sessions
            user_load = @($userLoad)
            # Vivacite pendant le traitement d'un lot (v2.26.0) : sans ce champ,
            # un agent qui execute une longue file ne heartbeate plus et devient
            # indiscernable d'un agent MORT (observed on a broker host, 12 min
            # de silence alors qu'il travaillait). "Occupe" doit se dire.
            busy = $busyInfo
            services = @($rdsServices | Where-Object { $_ })
            user_profiles = @($profiles)
            office_licenses = $o365
        }
    }
    catch {
        Write-AgentLog "Erreur collecte metriques: $_" -Level "ERROR"
        return $null
    }
}

function Invoke-DirectHttp {
    <#
    .SYNOPSIS
        Requete HTTP GET/POST sans proxy (immunise WinINET/WPAD en contexte SYSTEM).
    .DESCRIPTION
        Invoke-RestMethod/Invoke-WebRequest relisent le proxy IE meme avec
        DefaultWebProxy=$null (prouve 2026-06-22 : faux 200, master ne recoit rien).
        On cree un HttpWebRequest et on pose .Proxy=$null SUR L'OBJET = DIRECT garanti,
        equivalent au TcpClient brut valide en test. Retourne le corps (string) en mode
        normal, $true si -OutFile, leve une exception sur echec reseau/HTTP (le caller
        log en WARN). Aucune dependance proxy.
    #>
    param(
        [Parameter(Mandatory)][string]$Uri,
        [string]$Method = "POST",
        [string]$Body,
        [hashtable]$Headers = @{},
        [int]$TimeoutSec = 5,
        [string]$OutFile
    )
    $req = [System.Net.HttpWebRequest]::Create($Uri)
    $req.Method           = $Method
    $req.Proxy            = $null               # <- LA cle : DIRECT, jamais WinINET
    $req.Timeout          = $TimeoutSec * 1000
    $req.ReadWriteTimeout = $TimeoutSec * 1000
    $req.KeepAlive        = $false
    foreach ($k in $Headers.Keys) {
        if ($k -ieq "Content-Type") { $req.ContentType = [string]$Headers[$k] }
        else { $req.Headers.Add($k, [string]$Headers[$k]) }
    }
    if ($Method -eq "POST") {
        if (-not $req.ContentType) { $req.ContentType = "application/json" }
        $bytes = [Text.Encoding]::UTF8.GetBytes([string]$Body)
        $req.ContentLength = $bytes.Length
        $rs = $req.GetRequestStream()
        $rs.Write($bytes, 0, $bytes.Length)
        $rs.Close()
    }
    $resp   = $req.GetResponse()
    $stream = $resp.GetResponseStream()
    try {
        if ($OutFile) {
            $fs = [System.IO.File]::Create($OutFile)
            try { $stream.CopyTo($fs) } finally { $fs.Close() }
            return $true
        }
        $sr = New-Object System.IO.StreamReader($stream, [Text.Encoding]::UTF8)
        try { return $sr.ReadToEnd() } finally { $sr.Close() }
    }
    finally { $resp.Close() }
}

function Send-MetricsToBroker {
    param([hashtable]$Metrics)

    try {
        $json = $Metrics | ConvertTo-Json -Depth 5 -Compress
        $raw  = Invoke-DirectHttp -Uri $BrokerUrl -Method POST -Body $json `
                    -Headers @{ "X-Agent-Token" = $Token; "Content-Type" = "application/json" } -TimeoutSec 5
        Write-AgentLog "Metriques envoyees: CPU=$($Metrics.cpu)% RAM=$($Metrics.ram.UsedPercent)%"
        if ($raw) { return ($raw | ConvertFrom-Json) }
        return $null
    }
    catch {
        Write-AgentLog "Erreur envoi: $_" -Level "WARN"
        return $null
    }
}

# --- Etat "occupe" (v2.26.0) ------------------------------------------------
$script:BusyRemaining = 0
$script:BusyCurrent   = ''
$script:LastBusyBeat  = [datetime]::MinValue
$script:BusyBeatSeconds = 20

function Send-BusyHeartbeat {
    <#
      Heartbeat de vivacite emis ENTRE deux commandes d'un lot.
      Throttle a 20 s : Get-SystemMetrics coute quelques secondes, on ne le paie
      pas a chaque commande.
      🔴 Les commandes renvoyees par ce heartbeat sont RETOURNEES a l'appelant qui
      les empile dans la file en cours. Les ignorer les perdrait definitivement :
      cote master, une commande servie passe 'sent' et n'est jamais reservie
      (fix 2.24.5). Un heartbeat ne doit jamais avaler du travail.
    #>
    param([int]$Remaining, [string]$Current)
    if (((Get-Date) - $script:LastBusyBeat).TotalSeconds -lt $script:BusyBeatSeconds) { return @() }
    $script:LastBusyBeat = Get-Date
    try {
        $script:BusyRemaining = $Remaining
        $script:BusyCurrent   = $Current
        $m = Get-SystemMetrics
        if (-not $m) { return @() }
        $resp = Send-MetricsToBroker -Metrics $m
        if ($resp -and $resp.commands) { return @($resp.commands) }
    } catch {
        Write-AgentLog "Heartbeat de vivacite echoue: $_" -Level "WARN"
    }
    return @()
}

function Handle-BrokerCommands {
    param([Object]$Response)

    if (-not $Response -or -not $Response.commands) { return }

    # File explicite (et non foreach) : elle permet d'empiler les commandes
    # recues par les heartbeats de vivacite emis en cours de traitement.
    $queue = [System.Collections.Generic.Queue[object]]::new()
    foreach ($c in $Response.commands) { $queue.Enqueue($c) }

    while ($queue.Count -gt 0) {
        $cmd = $queue.Dequeue()
        $script:BusyRemaining = $queue.Count + 1
        $script:BusyCurrent   = "$($cmd.action)"
        $cmdId = $cmd.id
        $action = $cmd.action
        $params = $cmd.params
        
        Write-AgentLog "Traitement commande: $action ($cmdId)" -Level "INFO"
        
        $resultStatus = "error"
        $resultMsg = "Action non supportee ou erreur initiale"
        
        try {
            # Log de l'action pour debug direct dans les logs agent
            Write-AgentLog "Execution action: $action avec params: $(ConvertTo-Json $params -Compress)" -Level "DEBUG"

            switch ($action) {   # insensible a la casse par defaut ; -CaseInsensitive n'existe pas en PS<7
                "logoff" {
                    # Cible OBLIGATOIRE : `logoff` sans argument agit sur la session
                    # courante. Meme famille que le bug `msg *` (2.25.4) : une action
                    # ciblee sans cible ne doit rien faire, pas "quelque chose".
                    $sessionID = $params.sessionId
                    if (-not $sessionID) { $sessionID = $params.SessionId }
                    if ("$sessionID" -notmatch '^\d+$') {
                        $resultStatus = "error"
                        $resultMsg = "logoff: sessionId absent ou invalide - refuse"
                    } else {
                        $out = (& logoff $sessionID 2>&1 | Out-String).Trim()
                        if ($LASTEXITCODE -eq 0) {
                            $resultStatus = "success"; $resultMsg = "Session $sessionID deconnectee"
                        } else {
                            $resultStatus = "error"; $resultMsg = "logoff session $sessionID : echec (code $LASTEXITCODE) $out"
                        }
                    }
                }
                "disconnect" {
                    $sessionID = $params.sessionId
                    if (-not $sessionID) { $sessionID = $params.SessionId }
                    if ("$sessionID" -notmatch '^\d+$') {
                        $resultStatus = "error"
                        $resultMsg = "disconnect: sessionId absent ou invalide - refuse"
                    } else {
                        $out = (& tsdiscon $sessionID 2>&1 | Out-String).Trim()
                        if ($LASTEXITCODE -eq 0) {
                            $resultStatus = "success"; $resultMsg = "Session $sessionID basculee en deconnecte"
                        } else {
                            $resultStatus = "error"; $resultMsg = "disconnect session $sessionID : echec (code $LASTEXITCODE) $out"
                        }
                    }
                }
                "shadow" {
                    $sessionID = $params.sessionId
                    if (-not $sessionID) { $sessionID = $params.SessionId }
                    $resultStatus = "success"
                    $resultMsg = "SESSION_SHADOW_INFO|$script:Hostname|$sessionID"
                }
                "screenshot" {
                    try {
                        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
                        $screen = [System.Windows.Forms.Screen]::PrimaryScreen
                        if ($null -eq $screen) { throw "Aucun ecran detecte (Session 0 / Headless)" }
                        
                        $width  = $screen.Bounds.Width
                        $height = $screen.Bounds.Height
                        $bitmap  = New-Object System.Drawing.Bitmap -ArgumentList $width, $height
                        $graphic = [System.Drawing.Graphics]::FromImage($bitmap)
                        $graphic.CopyFromScreen($screen.Bounds.Left, $screen.Bounds.Top, 0, 0, $bitmap.Size)
                        
                        $ms = New-Object System.IO.MemoryStream
                        $bitmap.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
                        $base64 = [Convert]::ToBase64String($ms.ToArray())
                        
                        $bitmap.Dispose(); $graphic.Dispose(); $ms.Dispose()
                        $resultStatus = "success"
                        $resultMsg = "data:image/jpeg;base64,$base64"
                    } catch {
                        $resultStatus = "error"
                        $resultMsg = "Erreur capture (Isolation Session 0): $($_.Exception.Message)"
                    }
                }
                "message" {
                    # 🔴 Jusqu'en 2.25.3 : `msg *` = TOUTES les sessions de l'hote.
                    # Un message destine a UN utilisateur a ete recu par les 7 users de l'hote
                    # On cible la SESSION (id), a defaut le USER
                    # (msg <user> ne vise que ses sessions) — et sans cible on REFUSE :
                    # une diffusion ne doit jamais etre un comportement par defaut.
                    $msgText = "$($params.message -replace '"', "'")"
                    $user = $params.user
                    $sid = "$($params.sessionId)"
                    $target = $null
                    if ($sid -match '^\d+$') { $target = $sid }
                    elseif ($user) { $target = $user }
                    if ($null -eq $target) {
                        $resultStatus = "error"
                        $resultMsg = "message: aucune cible (sessionId/user manquants) - envoi refuse pour ne pas diffuser a tout l'hote"
                    } else {
                        $msgOut = (& msg $target /SERVER:localhost /TIME:120 $msgText 2>&1 | Out-String).Trim()
                        if ($LASTEXITCODE -eq 0) {
                            $resultStatus = "success"
                            $resultMsg = "Message envoye a $user (cible: $target)"
                        } else {
                            $resultStatus = "error"
                            $resultMsg = "message: echec msg vers '$target' (code $LASTEXITCODE) $msgOut"
                        }
                    }
                }
                "clean_downloads" {
                    # Params : days (def. 14), dry_run (def. TRUE), profile (optionnel).
                    # ⚠️ dry_run doit valoir explicitement $false pour supprimer.
                    try {
                        $d = if ($params.days) { [int]$params.days } else { 14 }
                        $dry = $true
                        if ($null -ne $params.dry_run) { $dry = [bool]$params.dry_run }
                        $prof = if ($params.profile) { [string]$params.profile } else { '' }
                        $res = Invoke-DownloadsCleanup -Days $d -DryRun $dry -Profile $prof
                        $resultStatus = "success"
                        $resultMsg = ($res | ConvertTo-Json -Depth 4 -Compress)
                    } catch {
                        $resultStatus = "error"; $resultMsg = "clean_downloads: $($_.Exception.Message)"
                    }
                }
                "scan_profiles_sizes" {
                    # Calcul des tailles de TOUS les profils (~37 s pour 101 sur R:).
                    # Bloque quelques heartbeats : sans effet, le seuil hors-ligne est a 120 s.
                    try {
                        Update-ProfileSizes
                        $n = $script:ProfileSizes.Count
                        $tot = 0; foreach ($v in $script:ProfileSizes.Values) { if ($v -is [double] -or $v -is [int]) { $tot += $v } }
                        $resultStatus = "success"
                        $resultMsg = "$n profil(s) mesure(s), total $([math]::Round($tot/1024,1)) Go. Les tailles remontent au prochain heartbeat."
                    } catch {
                        $resultStatus = "error"; $resultMsg = "scan_profiles_sizes: $($_.Exception.Message)"
                    }
                }
                "get_profiles" {
                    $profileList = @()
                    if (Test-Path "C:\Users") {
                        $dirs = Get-ChildItem -Path "C:\Users" -Directory -Exclude "Public", "Default*", "Administrator"
                        foreach ($dir in $dirs) {
                            $profileList += [PSCustomObject]@{
                                Name = $dir.Name
                                LastWriteTime = $dir.LastWriteTime.ToString("yyyy-MM-dd HH:mm")
                                SizeMB = "Scan requis"
                            }
                        }
                    }
                    $resultStatus = "success"
                    $resultMsg = ConvertTo-Json @($profileList) -Compress
                }
                "get_profile_size" {
                    $profileName = $params.name
                    $path = "C:\Users\$profileName"
                    if (Test-Path $path) {
                        $size = (Get-ChildItem -Path $path -Recurse -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum / 1MB
                        $resultStatus = "success"
                        $resultMsg = [Math]::Round($size, 1).ToString() + " MB"
                    } else {
                        $resultStatus = "error"
                        $resultMsg = "Profil introuvable"
                    }
                }
                "clean_profile" {
                    $profileName = $params.name
                    $tempPath = "C:\Users\$profileName\AppData\Local\Temp"
                    if (Test-Path $tempPath) {
                        Remove-Item "$tempPath\*" -Recurse -Force -ErrorAction SilentlyContinue
                        $resultStatus = "success"
                        $resultMsg = "Profil $profileName nettoye"
                    } else {
                        $resultStatus = "success"
                        $resultMsg = "Dossier temp absent ou inaccessible pour $profileName"
                    }
                }
                "analyze_profile" {
                    # Analyser la repartition de l'espace disque par dossier
                    $profileName = $params.name
                    $analysis = Get-ProfileFolderAnalysis -ProfileName $profileName
                    $resultStatus = "success"
                    $resultMsg = ConvertTo-Json $analysis -Compress
                }
                "restart_agent" {
                    # Redemarrer la tache planifiee SysAdminMasterAgent via schtasks
                    # (Stop/Start-ScheduledTask absent en PS < 7 sur vieux RDS — v2.9.0)
                    # 🔴 Le redemarrage DOIT etre detache. En 2.23 et avant, cette action
                    # appelait `schtasks /End` en ligne, sur SA PROPRE tache : elle se tuait
                    # elle-meme, le `/Run` de la ligne suivante n'etait jamais atteint, et
                    # le resultat n'etait jamais renvoye (commande figee en `pending`).
                    # L'agent restait mort jusqu'a un reboot : le declencheur de la tache est
                    # `-AtStartup` seul, et `-RestartCount` ne couvre PAS un arret par /End
                    # (Windows le compte comme terminaison demandee, pas comme echec).
                    # PROUVE sur un broker : agent tombe, reprise a distance
                    # impossible (`orchestrate_bootstrap` depuis un autre hote -> Acces refuse,
                    # aucun compte machine RDS n'est admin sur le broker). Il a fallu la main.
                    # L'auto-heal (plus bas) faisait deja les choses correctement.
                    try {
                        Write-AgentLog "Redemarrage detache de SysAdminMasterAgent..." -Level "INFO"
                        $restartCmd = "Start-Sleep -Seconds 5; " +
                                      "schtasks /End /TN 'SysAdminMasterAgent'; " +
                                      "Start-Sleep -Seconds 3; " +
                                      "schtasks /Run /TN 'SysAdminMasterAgent'"
                        Start-Process powershell -ArgumentList "-NoProfile -Command `"$restartCmd`"" -WindowStyle Hidden
                        $resultStatus = "success"
                        $resultMsg = "Redemarrage programme (detache) - l'agent revient sous ~15 s"
                        Write-AgentLog $resultMsg -Level "INFO"
                    }
                    catch {
                        $resultStatus = "error"
                        $resultMsg = "Impossible de redemarrer la tache: $_"
                        Write-AgentLog $resultMsg -Level "ERROR"
                    }
                }
                "update_agent" {
                    # Auto-mise à jour (modèle pull) : télécharge le nouvel agent + installeur
                    # depuis le master, puis réinstalle en re-pointant le BrokerUrl.
                    # 100 % HTTP — aucun WinRM. Permet de basculer la ferme depuis Linux.
                    try {
                        # 1. Nouveau broker (endpoint metrics) — fallback = broker actuel
                        $newBroker = $params.broker_url
                        if (-not $newBroker) { $newBroker = $BrokerUrl }

                        # 2. Token — fallback = token actuel
                        $newToken = $params.token
                        if (-not $newToken) { $newToken = $Token }

                        # 3. Base de téléchargement des scripts — fallback = derivé du nouveau broker
                        $scriptBase = $params.script_base_url
                        if (-not $scriptBase) { $scriptBase = $newBroker -replace '/api/agent/metrics/?$', '/scripts' }
                        $scriptBase = $scriptBase.TrimEnd('/')

                        Write-AgentLog "update_agent: source=$scriptBase nouveau broker=$newBroker" -Level "INFO"

                        # 4. Dossier temporaire de mise à jour
                        $tmpDir = Join-Path $env:TEMP "sam-update-$cmdId"
                        New-Item -Path $tmpDir -ItemType Directory -Force | Out-Null

                        $hdr = @{ "X-Agent-Token" = $newToken }
                        $agentDst   = Join-Path $tmpDir "SysAdminMaster-Agent.ps1"
                        $installDst = Join-Path $tmpDir "Install-SysAdminMaster-Agent.ps1"

                        # 5. Téléchargement (l'installeur attend l'agent dans le MÊME dossier via $PSScriptRoot)
                        Invoke-DirectHttp -Uri "$scriptBase/SysAdminMaster-Agent.ps1" -Method GET -Headers $hdr -OutFile $agentDst -TimeoutSec 20 | Out-Null
                        Invoke-DirectHttp -Uri "$scriptBase/Install-SysAdminMaster-Agent.ps1" -Method GET -Headers $hdr -OutFile $installDst -TimeoutSec 20 | Out-Null

                        if (-not (Test-Path $agentDst) -or -not (Test-Path $installDst)) {
                            throw "Téléchargement incomplet (agent ou installeur manquant)"
                        }

                        # ---- PARSE-CHECK BLOQUANT (garde-fou pose le 30/07/2026) ----
                        # Verifier que le fichier EXISTE ne dit rien de sa VALIDITE.
                        # Le 23/06/2026, une fleet-update a diffuse un agent portant une
                        # erreur de syntaxe : chaque hote s'est remplace par un script qui
                        # ne demarre pas, et la ferme entiere (10 hotes) est tombee en
                        # 12 secondes -- la commande ayant repondu "success". On refuse
                        # desormais de reinstaller un script qui ne compile pas : l'agent
                        # en place continue de tourner et l'erreur reelle remonte.
                        foreach ($fileToCheck in @($agentDst, $installDst)) {
                            $parseErrors = $null
                            [void][System.Management.Automation.Language.Parser]::ParseFile($fileToCheck, [ref]$null, [ref]$parseErrors)
                            if ($parseErrors -and $parseErrors.Count -gt 0) {
                                $leaf = Split-Path $fileToCheck -Leaf
                                $detail = ($parseErrors | Select-Object -First 3 | ForEach-Object { $_.Message }) -join ' | '
                                throw "Script telecharge INVALIDE ($leaf) : $($parseErrors.Count) erreur(s) de parsing. Mise a jour ABANDONNEE, agent en place conserve. $detail"
                            }
                        }
                        Write-AgentLog "parse-check OK (agent + installeur valides)" -Level "INFO"

                        # 6. Lancer l'installeur en PROCESSUS DÉTACHÉ : il va désenregistrer + recréer
                        #    la tâche planifiée qui exécute CE process. Le délai laisse d'abord partir
                        #    le résultat de commande ci-dessous (sinon la tâche est tuée avant le POST).
                        $reinstall = "Start-Sleep -Seconds 6; & '$installDst' -ComputerName localhost -BrokerUrl '$newBroker' -Token '$newToken' -IntervalSeconds $IntervalSeconds *> '$tmpDir\update.log'"
                        Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-Command",$reinstall -WindowStyle Hidden

                        $resultStatus = "success"
                        $resultMsg = "Mise à jour lancée (broker=$newBroker). Réinstallation en cours, l'agent va redémarrer."
                        Write-AgentLog $resultMsg -Level "INFO"
                    }
                    catch {
                        $resultStatus = "error"
                        $resultMsg = "Echec update_agent: $($_.Exception.Message)"
                        Write-AgentLog $resultMsg -Level "ERROR"
                    }
                }
                "orchestrate_bootstrap" {
                    # Déploie l'agent sur un host RDS distant depuis SYSTEM (machine account Kerberos).
                    # Télécharge scripts depuis AgentMaster /scripts/, puis Invoke-Command sur la cible.
                    # Nécessite que le compte machine du broker (<BROKER>$) soit dans les admins locaux de la cible (GPO Restricted Groups).
                    try {
                        $targetHost = if ($params.target) { $params.target } else { '' }
                        if (-not $targetHost) { throw "Paramètre 'target' manquant dans params" }

                        $newBroker = if ($params.broker_url) { $params.broker_url } else { $BrokerUrl }
                        $newToken  = if ($params.token)      { $params.token      } else { $Token }
                        $scriptBase = ($newBroker -replace '/api/agent/metrics/?$', '/scripts').TrimEnd('/')

                        $tmpDir = Join-Path $env:TEMP "sam-bootstrap-$cmdId"
                        New-Item -Path $tmpDir -ItemType Directory -Force | Out-Null

                        $hdr = @{ "X-Agent-Token" = $newToken }
                        $agentDst   = Join-Path $tmpDir "SysAdminMaster-Agent.ps1"
                        $installDst = Join-Path $tmpDir "Install-SysAdminMaster-Agent.ps1"

                        Write-AgentLog "orchestrate_bootstrap: téléchargement scripts depuis $scriptBase pour $targetHost" -Level "INFO"
                        Invoke-DirectHttp -Uri "$scriptBase/SysAdminMaster-Agent.ps1"       -Method GET -Headers $hdr -OutFile $agentDst   -TimeoutSec 20 | Out-Null
                        Invoke-DirectHttp -Uri "$scriptBase/Install-SysAdminMaster-Agent.ps1" -Method GET -Headers $hdr -OutFile $installDst -TimeoutSec 20 | Out-Null

                        if (-not (Test-Path $agentDst) -or -not (Test-Path $installDst)) {
                            throw "Téléchargement incomplet (agent ou installeur manquant)"
                        }

                        # ---- PARSE-CHECK BLOQUANT (garde-fou pose le 30/07/2026) ----
                        # Verifier que le fichier EXISTE ne dit rien de sa VALIDITE.
                        # Le 23/06/2026, une fleet-update a diffuse un agent portant une
                        # erreur de syntaxe : chaque hote s'est remplace par un script qui
                        # ne demarre pas, et la ferme entiere (10 hotes) est tombee en
                        # 12 secondes -- la commande ayant repondu "success". On refuse
                        # desormais de reinstaller un script qui ne compile pas : l'agent
                        # en place continue de tourner et l'erreur reelle remonte.
                        foreach ($fileToCheck in @($agentDst, $installDst)) {
                            $parseErrors = $null
                            [void][System.Management.Automation.Language.Parser]::ParseFile($fileToCheck, [ref]$null, [ref]$parseErrors)
                            if ($parseErrors -and $parseErrors.Count -gt 0) {
                                $leaf = Split-Path $fileToCheck -Leaf
                                $detail = ($parseErrors | Select-Object -First 3 | ForEach-Object { $_.Message }) -join ' | '
                                throw "Script telecharge INVALIDE ($leaf) : $($parseErrors.Count) erreur(s) de parsing. Mise a jour ABANDONNEE, agent en place conserve. $detail"
                            }
                        }
                        Write-AgentLog "parse-check OK (agent + installeur valides)" -Level "INFO"

                        Write-AgentLog "orchestrate_bootstrap: exécution installeur sur $targetHost" -Level "INFO"
                        # Invoke-Command utilise <BROKER>$ (SYSTEM machine Kerberos) pour WinRM.
                        # Prérequis : <BROKER>$ membre du groupe Admins locaux sur la cible (GPO).
                        $agentContent = Get-Content $agentDst -Raw
                        $output = Invoke-Command -ComputerName $targetHost -ErrorAction Stop -ScriptBlock {
                            param($AgentScriptContent, $Broker, $Interval, $TokenValue)
                            $InstallPath = "C:\Program Files\SysAdminMaster"
                            $taskName = "SysAdminMasterAgent"
                            if (-not (Test-Path $InstallPath)) { New-Item -Path $InstallPath -ItemType Directory -Force | Out-Null }
                            $AgentScriptContent | Out-File -FilePath "$InstallPath\SysAdminMaster-Agent.ps1" -Encoding UTF8 -Force
                            $wrapper = "Set-Location '$InstallPath'`n& '$InstallPath\SysAdminMaster-Agent.ps1' -BrokerUrl '$Broker' -IntervalSeconds $Interval -Token '$TokenValue'"
                            $wrapper | Out-File -FilePath "$InstallPath\Run-Agent.ps1" -Encoding UTF8 -Force
                            $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                            if ($existing) { Stop-ScheduledTask $taskName -ErrorAction SilentlyContinue; Unregister-ScheduledTask $taskName -Confirm:$false }
                            Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -like "*SysAdminMaster*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
                            $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstallPath\Run-Agent.ps1`""
                            $trigger = New-ScheduledTaskTrigger -AtStartup
                            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Hours 0)
                            $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
                            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
                            Start-ScheduledTask -TaskName $taskName
                            "Agent installé et démarré sur $env:COMPUTERNAME (broker=$Broker)"
                        } -ArgumentList $agentContent, $newBroker, $IntervalSeconds, $newToken 2>&1

                        Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
                        $resultStatus = "success"
                        $resultMsg = "[OK] Bootstrap $targetHost : $output"
                        Write-AgentLog $resultMsg -Level "INFO"
                    }
                    catch {
                        Remove-Item (Join-Path $env:TEMP "sam-bootstrap-$cmdId") -Recurse -Force -ErrorAction SilentlyContinue
                        $resultStatus = "error"
                        $resultMsg = "orchestrate_bootstrap $($params.target): $($_.Exception.Message)"
                        Write-AgentLog $resultMsg -Level "ERROR"
                    }
                }
                "renew_office_license" {
                    # Panne 1/2 : jeton SCA perime (> 30 jours).
                    # Params: user (obligatoire)
                    try {
                        $ctx = Assert-OfficeRepairReady -UserName $params.user
                        $resultMsg = Invoke-OfficeLicenseRenewal -Ctx $ctx
                        Remove-OldOfficeBackups
                        $resultStatus = "success"
                        Write-AgentLog $resultMsg -Level "INFO"
                    }
                    catch {
                        $resultStatus = "error"
                        $resultMsg = "renew_office_license echoue: $_"
                        Write-AgentLog $resultMsg -Level "ERROR"
                    }
                }
                "repair_office_identity" {
                    # Panne 2/2 : cache d'identite corrompu, jeton valide.
                    # Symptome type : erreur Office [48v35] / code 2147942402
                    # (0x80070002, "fichier introuvable") a l'authentification.
                    # Params: user (obligatoire)
                    try {
                        $ctx = Assert-OfficeRepairReady -UserName $params.user
                        $resultMsg = Invoke-OfficeIdentityRepair -Ctx $ctx
                        Remove-OldOfficeBackups
                        $resultStatus = "success"
                        Write-AgentLog $resultMsg -Level "INFO"
                    }
                    catch {
                        $resultStatus = "error"
                        $resultMsg = "repair_office_identity echoue: $_"
                        Write-AgentLog $resultMsg -Level "ERROR"
                    }
                }
                "repair_office_token" {
                    # HISTORIQUE (v2.9.0 -> v2.27.x), conserve pour compatibilite :
                    # un dashboard non encore mis a jour appelle encore ce nom.
                    # Delegue aux deux actions nommees. Ne PAS l'utiliser dans du
                    # code neuf : "reparer le jeton" est justement le raccourci qui
                    # a fait diagnostiquer un jeton perime sur un cache d'identite
                    # corrompu (31/08/2026).
                    # Params: user (obligatoire), include_identity (optionnel bool)
                    try {
                        $ctx = Assert-OfficeRepairReady -UserName $params.user
                        $msgs = @(Invoke-OfficeLicenseRenewal -Ctx $ctx)
                        if ([bool]$params.include_identity) {
                            $msgs += Invoke-OfficeIdentityRepair -Ctx $ctx
                        }
                        Remove-OldOfficeBackups
                        $resultStatus = "success"
                        $resultMsg = "[action historique - preferer renew_office_license / repair_office_identity] " + ($msgs -join ' | ')
                        Write-AgentLog $resultMsg -Level "INFO"
                    }
                    catch {
                        $resultStatus = "error"
                        $resultMsg = "repair_office_token echoue: $_"
                        Write-AgentLog $resultMsg -Level "ERROR"
                    }
                }
                "diagnose_office_auth" {
                    # Panne 3/3, volet DIAGNOSTIC. LECTURE SEULE : n'exige ni
                    # Office ferme ni profil monte, justement pour rester
                    # disponible quand la reparation, elle, ne l'est pas.
                    # Params: user (obligatoire), hours (optionnel, defaut 6)
                    try {
                        $h = 6
                        if ($params.hours) { $h = [int]$params.hours }
                        $resultMsg = Get-OfficeAuthDiagnostic -UserName $params.user -Hours $h
                        $resultStatus = "success"
                        Write-AgentLog "diagnose_office_auth $($params.user) : OK" -Level "INFO"
                    }
                    catch {
                        $resultStatus = "error"
                        $resultMsg = "diagnose_office_auth echoue: $_"
                        Write-AgentLog $resultMsg -Level "ERROR"
                    }
                }
                "repair_office_broker" {
                    # Panne 3/3 : caches de jetons du broker Azure AD en boucle
                    # (AADSTS50196). Ni le jeton ni le cache d'identite Office
                    # ne sont en cause - ce sont OneAuth et TokenBroker.
                    # Params: user (obligatoire)
                    try {
                        $ctx = Assert-OfficeRepairReady -UserName $params.user
                        $resultMsg = Invoke-OfficeBrokerReset -Ctx $ctx
                        Remove-OldOfficeBackups
                        $resultStatus = "success"
                        Write-AgentLog $resultMsg -Level "INFO"
                    }
                    catch {
                        $resultStatus = "error"
                        $resultMsg = "repair_office_broker echoue: $_"
                        Write-AgentLog $resultMsg -Level "ERROR"
                    }
                }
                "clear_temp" {
                    # Nettoyage fichiers temporaires systeme et profils utilisateurs
                    try {
                        $paths = @("$env:SystemRoot\Temp", "$env:TEMP")
                        $cleaned = 0; $failed = 0; $totalBytes = 0
                        foreach ($p in $paths) {
                            if (Test-Path $p) {
                                Get-ChildItem -Path $p -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
                                    $sz = if ($_.PSIsContainer) { 0 } else { $_.Length }
                                    try { Remove-Item $_.FullName -Force -Recurse -ErrorAction Stop; $cleaned++; $totalBytes += $sz } catch { $failed++ }
                                }
                            }
                        }
                        $mb = [math]::Round($totalBytes / 1MB, 1)
                        $resultStatus = "success"
                        $resultMsg = "[OK] Nettoyage $env:COMPUTERNAME : $cleaned fichier(s) / $($mb) Mo liberes ($failed verrouille(s))"
                    } catch {
                        $resultStatus = "error"; $resultMsg = "clear_temp: $($_.Exception.Message)"
                    }
                }
                "optimize_ram" {
                    # Liberation RAM : vide les working sets des processus via EmptyWorkingSet API
                    try {
                        $before = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1024, 0)
                        [System.GC]::Collect(); [System.GC]::WaitForPendingFinalizers()
                        $src = @'
using System; using System.Diagnostics; using System.Runtime.InteropServices;
public class WsHelper {
    [DllImport("psapi.dll")] public static extern bool EmptyWorkingSet(IntPtr h);
    public static int EmptyAll() { int n=0; foreach(var p in Process.GetProcesses()) { try { if(EmptyWorkingSet(p.Handle)) n++; } catch {} } return n; }
}
'@
                        Add-Type -TypeDefinition $src -ErrorAction SilentlyContinue
                        $count = [WsHelper]::EmptyAll()
                        Start-Sleep -Seconds 2
                        $after = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1024, 0)
                        $gain = $after - $before
                        $resultStatus = "success"
                        $gainStr = if($gain -gt 0) { "+$gain" } else { "$gain" }
                        $resultMsg = "[OK] RAM optimisee $env:COMPUTERNAME : $count processus vides, avant=$($before)Mo libre, apres=$($after)Mo libre, gain=${gainStr}Mo"
                    } catch {
                        $resultStatus = "error"; $resultMsg = "optimize_ram: $($_.Exception.Message)"
                    }
                }
                "restart_spooler" {
                    # Redemarrage service spouleur impression + purge file attente
                    try {
                        Stop-Service -Name Spooler -Force -ErrorAction SilentlyContinue
                        Start-Sleep -Seconds 1
                        $spoolDir = "$env:SystemRoot\System32\spool\PRINTERS"
                        $purged = 0
                        if (Test-Path $spoolDir) {
                            Get-ChildItem $spoolDir -ErrorAction SilentlyContinue | ForEach-Object {
                                Remove-Item $_.FullName -Force -Recurse -ErrorAction SilentlyContinue; $purged++
                            }
                        }
                        Start-Service -Name Spooler -ErrorAction Stop
                        Start-Sleep -Seconds 2
                        $svc = Get-Service -Name Spooler
                        $resultStatus = "success"
                        $resultMsg = "[OK] Spouleur $env:COMPUTERNAME : statut=$($svc.Status), $purged travail(s) purge(s). Pret."
                    } catch {
                        $resultStatus = "error"; $resultMsg = "restart_spooler: $($_.Exception.Message)"
                    }
                }
                "flush_dns" {
                    # Vider le cache DNS local
                    try {
                        ipconfig /flushdns 2>&1 | Out-Null
                        Clear-DnsClientCache -ErrorAction SilentlyContinue
                        $resultStatus = "success"
                        $resultMsg = "[OK] Cache DNS vide sur $env:COMPUTERNAME. Resolution DNS operationnelle."
                    } catch {
                        $resultStatus = "error"; $resultMsg = "flush_dns: $($_.Exception.Message)"
                    }
                }
                "global_maintenance" {
                    # Cycle complet : clear_temp + optimize_ram + restart_spooler + flush_dns
                    try {
                        $report = @("[*] Maintenance complete $env:COMPUTERNAME @ $(Get-Date -Format 'HH:mm:ss')")
                        # 1. TEMP
                        $c = 0; $b = 0
                        @("$env:SystemRoot\Temp","$env:TEMP") | ForEach-Object {
                            if (Test-Path $_) {
                                Get-ChildItem -Path $_ -Recurse -Force -EA SilentlyContinue | ForEach-Object {
                                    $sz = if ($_.PSIsContainer){0} else {$_.Length}
                                    try { Remove-Item $_.FullName -Force -Recurse -EA Stop; $c++; $b+=$sz } catch {}
                                }
                            }
                        }
                        $report += "[1/4] TEMP : $c fichier(s) / $([math]::Round($b/1MB,1)) Mo liberes"
                        # 2. RAM
                        [System.GC]::Collect()
                        $ramBefore = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1024,0)
                        $report += "[2/4] RAM : $($ramBefore)Mo disponibles"
                        # 3. Spooler
                        Stop-Service Spooler -Force -EA SilentlyContinue
                        $sq = 0
                        Get-ChildItem "$env:SystemRoot\System32\spool\PRINTERS" -EA SilentlyContinue | ForEach-Object { Remove-Item $_.FullName -Force -Recurse -EA SilentlyContinue; $sq++ }
                        Start-Service Spooler -EA SilentlyContinue
                        $report += "[3/4] SPOOLER : $sq travail(s) purge(s), redemarrage OK"
                        # 4. DNS
                        ipconfig /flushdns 2>&1 | Out-Null; Clear-DnsClientCache -EA SilentlyContinue
                        $report += "[4/4] DNS : cache vide"
                        $resultStatus = "success"; $resultMsg = $report -join "`n"
                    } catch {
                        $resultStatus = "error"; $resultMsg = "global_maintenance: $($_.Exception.Message)"
                    }
                }
                "diagnose_rds" {
                    # Diagnostic services RDS + evenements recents
                    try {
                        $report = @("=== Diagnostic RDS : $env:COMPUTERNAME @ $(Get-Date -Format 'HH:mm:ss') ===","")
                        # Services
                        $rdsSvcs = @('TermService','UmRdpService','SessionEnv','CertPropSvc','RpcSs','LanmanServer','Netlogon')
                        $report += "[Services RDS]"
                        $rdsSvcs | ForEach-Object {
                            $s = Get-Service $_ -EA SilentlyContinue
                            $report += if ($s) { "  $($s.Name) = $($s.Status)" } else { "  $_ = INTROUVABLE" }
                        }
                        # Sessions
                        $sessions = (query session 2>&1) -join "`n"
                        $report += "","[Sessions actives]",$sessions
                        # Erreurs < 6h
                        $since = (Get-Date).AddHours(-6)
                        $evts = Get-WinEvent -LogName 'System','Application' -EA SilentlyContinue |
                            Where-Object { $_.Level -le 2 -and $_.TimeCreated -gt $since } |
                            Select-Object -First 8 |
                            ForEach-Object { "  [$($_.TimeCreated.ToString('HH:mm'))] $($_.ProviderName): $($_.Message.Split("`n")[0])" }
                        $report += "","[Erreurs < 6h]"
                        $report += if ($evts) { $evts } else { "  Aucune erreur critique." }
                        $resultStatus = "success"; $resultMsg = $report -join "`n"
                    } catch {
                        $resultStatus = "error"; $resultMsg = "diagnose_rds: $($_.Exception.Message)"
                    }
                }
                "audit_security" {
                    # Audit : admins locaux, Defender, UAC, GPO
                    try {
                        $report = @("=== Audit Securite : $env:COMPUTERNAME @ $(Get-Date -Format 'HH:mm:ss') ===","")
                        # Admins locaux
                        $admins = net localgroup Administrators 2>&1 | Where-Object { $_ -match '\\\\' -or ($env:USERDOMAIN -and $_ -match [regex]::Escape($env:USERDOMAIN)) }
                        $report += "[Administrateurs locaux]"
                        $report += if ($admins) { $admins | ForEach-Object { "  $_" } } else { "  (liste vide)" }
                        # Defender
                        $wd = Get-MpComputerStatus -EA SilentlyContinue
                        $report += "","[Windows Defender]"
                        if ($wd) {
                            $report += "  Protection temps reel : $($wd.RealTimeProtectionEnabled)"
                            $report += "  Sigs MAJ : $($wd.AntivirusSignatureLastUpdated.ToString('dd/MM/yyyy HH:mm'))"
                        } else { $report += "  Indisponible" }
                        # UAC
                        $uac = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -EA SilentlyContinue).EnableLUA
                        $report += "","[UAC] EnableLUA = $uac (1=active)"
                        # GPO
                        $gp = gpresult /r /scope computer 2>&1 | Select-String "Applied|Applique" | Select-Object -First 5
                        $report += "","[GPO appliquees]"
                        $report += if ($gp) { $gp | ForEach-Object { "  $($_.ToString().Trim())" } } else { "  gpresult indisponible" }
                        $resultStatus = "success"; $resultMsg = $report -join "`n"
                    } catch {
                        $resultStatus = "error"; $resultMsg = "audit_security: $($_.Exception.Message)"
                    }
                }
                "manage_profiles" {
                    # Inventaire + reparation droits profils utilisateurs
                    try {
                        $report = @("=== Profils Utilisateurs : $env:COMPUTERNAME @ $(Get-Date -Format 'HH:mm:ss') ===","")
                        $skip = @('Public','Default','Default User','All Users','LocalService','NetworkService')
                        $profiles = Get-ChildItem 'C:\Users' -Directory -EA SilentlyContinue | Where-Object { $_.Name -notin $skip }
                        $report += "$($profiles.Count) profil(s) :"
                        foreach ($p in $profiles) {
                            $size = [math]::Round((Get-ChildItem $p.FullName -Recurse -Force -EA SilentlyContinue | Measure-Object Length -Sum -EA SilentlyContinue).Sum / 1MB, 0)
                            $active = (query session 2>&1) | Where-Object { $_ -match $p.Name }
                            $state = if ($active) { "CONNECTE" } else { "hors ligne" }
                            $report += "  $($p.Name) - $($size)Mo [$state]"
                            # Verifie droits owner
                            $acl = Get-Acl $p.FullName -EA SilentlyContinue
                            if ($acl) {
                                $ok = $acl.Access | Where-Object { $_.IdentityReference -match $p.Name -and $_.FileSystemRights -match 'FullControl' }
                                if (-not $ok) {
                                    icacls $p.FullName /grant "$($p.Name):(OI)(CI)F" /T /Q 2>&1 | Out-Null
                                    $report += "    => Droits repares (icacls)"
                                }
                            }
                        }
                        $resultStatus = "success"; $resultMsg = $report -join "`n"
                    } catch {
                        $resultStatus = "error"; $resultMsg = "manage_profiles: $($_.Exception.Message)"
                    }
                }
                "manage_apps" {
                    # Inventaire applications installees + top processus CPU
                    try {
                        $report = @("=== Applications : $env:COMPUTERNAME @ $(Get-Date -Format 'HH:mm:ss') ===","")
                        $apps = Get-ItemProperty @(
                            'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
                        ) -EA SilentlyContinue |
                        Where-Object { $_.DisplayName -and $_.DisplayName.Trim() -ne '' } |
                        Select-Object DisplayName,DisplayVersion |
                        Sort-Object DisplayName -Unique
                        $report += "$($apps.Count) application(s):"
                        $apps | Select-Object -First 30 | ForEach-Object { $report += "  $($_.DisplayName)  v$($_.DisplayVersion)" }
                        if ($apps.Count -gt 30) { $report += "  ... ($(($apps.Count)-30) de plus)" }
                        # Top CPU
                        $procs = Get-Process | Sort-Object CPU -Descending | Select-Object -First 5
                        $report += "","[Top 5 processus CPU]"
                        $procs | ForEach-Object { $report += "  $($_.Name) (PID $($_.Id)) CPU=$([math]::Round($_.CPU,1))s" }
                        $resultStatus = "success"; $resultMsg = $report -join "`n"
                    } catch {
                        $resultStatus = "error"; $resultMsg = "manage_apps: $($_.Exception.Message)"
                    }
                }
                "run_ps_command" {
                    # Console interactive : execute une commande PS arbitraire (gate token)
                    try {
                        $psCmd = if ($params.command) { [string]$params.command } else { '' }
                        if (-not $psCmd) { throw "Parametre 'command' manquant" }
                        $blocked = @('Remove-Item','rm -rf','Format-','Invoke-Expression',' iex ','net user /add','net localgroup administrators')
                        foreach ($b in $blocked) { if ($psCmd -imatch [regex]::Escape($b)) { throw "Commande refusee (destructive) : $b" } }
                        $sb = [scriptblock]::Create($psCmd)
                        $out = & $sb 2>&1 | Out-String
                        $resultStatus = "success"; $resultMsg = $out.Trim()
                    } catch {
                        $resultStatus = "error"; $resultMsg = "run_ps_command: $($_.Exception.Message)"
                    }
                }
                default {
                    $resultStatus = "error"
                    $resultMsg = "Action '$action' inconnue par l'agent v$script:AgentVersion"
                }
            }
        }
        catch {
            $resultStatus = "error"
            $resultMsg = "Exception critique: $($_.Exception.Message)"
            Write-AgentLog "Erreur fatale commande $action : $resultMsg" -Level "ERROR"
        }
        
        # Envoyer le resultat au broker
        try {
            $resultUri = $BrokerUrl.Replace("/api/agent/metrics", "/api/agent/command-result")
            $payload = @{
                hostname = $env:COMPUTERNAME
                commandId = $cmdId
                status = $resultStatus
                message = $resultMsg
            }
            Invoke-DirectHttp -Uri $resultUri -Method POST -Body ($payload | ConvertTo-Json) `
                -Headers @{ "X-Agent-Token" = $Token; "Content-Type" = "application/json" } -TimeoutSec 5 | Out-Null
        }
        catch {
            Write-AgentLog "Echec envoi resultat commande: $_" -Level "ERROR"
        }

        # Vivacite : on signale qu'on travaille encore, et on recupere les
        # eventuelles nouvelles commandes pour ne pas les perdre.
        if ($queue.Count -gt 0) {
            foreach ($extra in (Send-BusyHeartbeat -Remaining $queue.Count -Current "$($cmd.action)")) {
                $queue.Enqueue($extra)
            }
        }
    }

    $script:BusyRemaining = 0
    $script:BusyCurrent   = ''
}

# === POINT D'ENTREE ===
Write-AgentLog "=== Agent SysAdminMaster demarre ===" -Level "INFO"
Write-AgentLog "Broker: $BrokerUrl | Intervalle: ${IntervalSeconds}s"

# Relecture du cache des tailles de profils AVANT le 1er heartbeat : sinon le
# premier POST repart avec « Scan requis » sur les 101 profils et la page
# clignote a chaque redemarrage d'agent.
Restore-ProfileSizeCache
Import-UserDnCache

if ($RunOnce) {
    # Mode test: une seule execution
    $metrics = Get-SystemMetrics
    if ($metrics) {
        Write-Host ($metrics | ConvertTo-Json -Depth 5)
        $resp = Send-MetricsToBroker -Metrics $metrics
        if ($resp) { Handle-BrokerCommands -Response $resp }
    }
}
else {
    # Mode service: boucle infinie
    Write-AgentLog "Mode service actif - Ctrl+C pour arreter"
    $script:FailureCount = 0
    $script:MaxFailures = 3

    while ($true) {
        $metrics = Get-SystemMetrics
        if ($metrics) {
            $resp = Send-MetricsToBroker -Metrics $metrics
            if ($resp) { 
                $script:FailureCount = 0 # Reset au succès
                Handle-BrokerCommands -Response $resp 
            }
            else {
                $script:FailureCount++
                Write-AgentLog "Echec de connexion au Broker ($script:FailureCount/$script:MaxFailures)" -Level "WARNING"
            }
        }

        # Mecanisme d'Auto-healing (v2.9.0 : schtasks remplace Stop/Start-ScheduledTask absent en PS<7)
        if ($script:FailureCount -ge $script:MaxFailures) {
            Write-AgentLog "Seuil d'echecs atteint. Tentative d'Auto-healing via schtasks..." -Level "CRITICAL"
            # Processus detache : schtasks /End + /Run sans bloquer la boucle courante
            $repairCmd = "Start-Sleep -Seconds 5; schtasks /End /TN 'SysAdminMasterAgent'; Start-Sleep -Seconds 2; schtasks /Run /TN 'SysAdminMasterAgent'"
            Start-Process powershell -ArgumentList "-NoProfile -Command `"$repairCmd`"" -WindowStyle Hidden
            Write-AgentLog "Commande de reparation lancee. Arret du processus actuel." -Level "INFO"
            break # Sortir de la boucle pour laisser la nouvelle tache prendre le relais
        }

        # --- Scan nocturne des tailles de profils ---------------------------
        # Une fois par jour a $script:ProfileScanHour. Sans ca, la page Profils
        # affiche « Scan requis » indefiniment et ne peut pas dire QUI remplit le
        # disque. Trop couteux a chaque heartbeat, negligeable une fois par nuit.
        $nowH = Get-Date
        $needScan = (-not $script:ProfileSizesAt) -or (($nowH - $script:ProfileSizesAt).TotalHours -ge 20)
        if ($needScan -and $nowH.Hour -eq $script:ProfileScanHour) {
            Write-AgentLog "Scan nocturne des tailles de profils (declenche a $($nowH.ToString('HH:mm')))" -Level "INFO"
            Update-ProfileSizes
        }

        Start-Sleep -Seconds $IntervalSeconds
    }
}
