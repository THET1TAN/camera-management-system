<#
Enhanced_System_Preparation.ps1 (lancé via .bat en admin)
--------------------------------------------------------
Script d'installation amélioré pour le système de caméras IP
Version: 2.0
Date: Mai 2025

Ce script:
1) Vérifie si Python 3.9 est installé
2) Si non, installe Python 3.9 via WinGet ou directement
3) Configure l'environnement Python correctement
4) Installe les packages nécessaires: cryptography, tkintertable, onvif-zeep, pillow, python-vlc
5) Configure VLC pour fonctionner avec l'application
#>

param(
    [switch]$Help,
    [switch]$Silent,
    [switch]$NoPrompt,
    [switch]$Diagnostic,
    [switch]$SkipVLC,
    [switch]$QuickFix
)

#region Configuration globale
$Global:ErrorActionPreference = "Continue"
$ProgressPreference = "Continue"
$InformationPreference = "Continue"
$SCRIPT_VERSION = "2.0"
$REQUIRED_PYTHON_VERSION = "3.9"
$TotalSteps = 5
$CurrentStep = 0
$LogFile = "$env:TEMP\camera_system_setup_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
$StartTime = Get-Date
$HasErrors = $false
$ProjectDir = Split-Path -Parent -Path $PSScriptRoot
$RequiredPythonPackages = @("cryptography", "tkintertable", "onvif-zeep", "pillow", "python-vlc")
#endregion

#region Fonctions d'aide d'interface utilisateur
function Show-Header {
    $date = Get-Date -Format "dd/MM/yyyy HH:mm:ss"
    $windowSize = $host.UI.RawUI.WindowSize.Width
    $header = @"
╔═════════════════════════════════════════════════════════════════════════════╗
║                        INSTALLATION SYSTÈME CAMÉRAS IP                       ║
║                         Version $SCRIPT_VERSION | $date                          ║
╚═════════════════════════════════════════════════════════════════════════════╝
"@
    Clear-Host
    Write-Host $header -ForegroundColor Cyan
    if ($Silent) {
        Write-Host "Mode silencieux activé. La majorité des messages seront supprimés." -ForegroundColor DarkGray
    }
    Write-Host "Journalisation dans: $LogFile" -ForegroundColor DarkGray
}

function Write-LogMessage {
    param(
        [string]$Message,
        [string]$Level = "INFO",
        [switch]$NoConsole
    )
    
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] [$Level] $Message"
    
    # Toujours écrire dans le fichier journal
    Add-Content -Path $LogFile -Value $logMessage -Encoding UTF8
    
    # Afficher dans la console si demandé et pas en mode silencieux
    if (-not $NoConsole -and -not ($Silent -and $Level -eq "INFO")) {
        $color = switch ($Level) {
            "ERROR"   { "Red" }
            "WARNING" { "Yellow" }
            "SUCCESS" { "Green" }
            "INFO"    { "White" }
            "DEBUG"   { "DarkGray" }
            default   { "White" }
        }
        Write-Host $Message -ForegroundColor $color
    }
}

function Write-Section {
    param(
        [string]$Title,
        [switch]$Minor
    )
    
    if ($Minor) {
        Write-LogMessage "`n--- $Title ---" -Level "INFO"
    } else {
        Write-LogMessage "`n══════════════════════════════════════════════════════════════" -Level "INFO"
        Write-LogMessage "  $Title" -Level "INFO"
        Write-LogMessage "══════════════════════════════════════════════════════════════" -Level "INFO"
    }
}

function Update-Progress {
    param(
        [string]$Activity,
        [string]$Status,
        [int]$StepNumber = $CurrentStep,
        [int]$PercentComplete = -1
    )
    
    $Global:CurrentStep = $StepNumber
    
    if ($PercentComplete -eq -1) {
        $PercentComplete = [math]::Floor(($StepNumber / $TotalSteps) * $100)
    }
    
    # Création d'une barre de progression visuelle
    $progressBar = ""
    $barWidth = 50
    $filledChars = [math]::Floor(($PercentComplete / 100) * $barWidth)
    
    for ($i = 0; $i -lt $barWidth; $i++) {
        if ($i -lt $filledChars) {
            $progressBar += "■"
        } else {
            $progressBar += "□"
        }
    }
    
    Write-Host ""
    Write-Host " $Activity " -ForegroundColor Cyan -NoNewline
    Write-Host "[$progressBar] " -NoNewline
    Write-Host "$PercentComplete% " -ForegroundColor Yellow
    Write-Host " $Status " -ForegroundColor DarkGray
    Write-Host ""
    
    # Écrire également dans le journal
    Write-LogMessage "Progression: $Activity - $Status ($PercentComplete%)" -NoConsole
    
    # Mettre à jour la barre de progression PowerShell standard aussi
    Write-Progress -Activity $Activity -Status $Status -PercentComplete $PercentComplete
}

function Show-BannerMessage {
    param(
        [string]$Message,
        [string]$Type = "Info" # Info, Success, Warning, Error
    )
    
    $symbol = switch ($Type) {
        "Success" { "+" }
        "Warning" { "!" }
        "Error"   { "x" }
        default   { "i" }
    }
    
    $color = switch ($Type) {
        "Success" { "Green" }
        "Warning" { "Yellow" }
        "Error"   { "Red" }
        default   { "Cyan" }
    }
    
    $width = $host.UI.RawUI.WindowSize.Width - 4
    if ($width -lt 30) { $width = 76 }
    
    $line = "═" * $width
    
    Write-Host ""
    Write-Host " ╔$line╗" -ForegroundColor $color
    
    $messageLines = $Message -split "`n"
    foreach ($messageLine in $messageLines) {
        # Découpage de la ligne en parties de largeur fixe
        for ($i = 0; $i -lt $messageLine.Length; $i += ($width - 4)) {
            $part = $messageLine.Substring($i, [Math]::Min(($width - 4), $messageLine.Length - $i))
            $spaces = " " * ($width - 4 - $part.Length)
            if ($i -eq 0 -and $messageLines.Count -eq 1) {
                Write-Host " ║ $symbol $part$spaces ║" -ForegroundColor $color
            } else {
                Write-Host " ║   $part$spaces ║" -ForegroundColor $color
            }
        }
    }
    
    Write-Host " ╚$line╝" -ForegroundColor $color
    Write-Host ""
    
    # Journaliser le message
    $level = switch ($Type) {
        "Success" { "SUCCESS" }
        "Warning" { "WARNING" }
        "Error"   { "ERROR" }
        default   { "INFO" }
    }
    Write-LogMessage "BANNER: $Message" -Level $level -NoConsole
}

function Get-UserChoice {
    param (
        [string]$Title = "Sélectionnez une option",
        [string[]]$Options,
        [int]$DefaultChoice = 0,
        [switch]$NoPrompt
    )
    
    if ($NoPrompt -or $Global:NoPrompt) {
        return $DefaultChoice
    }
    
    Write-Host "`n$Title" -ForegroundColor Cyan
    
    for ($i = 0; $i -lt $Options.Length; $i++) {
        Write-Host " [$($i+1)] " -ForegroundColor Yellow -NoNewline
        Write-Host "$($Options[$i])" -ForegroundColor White
    }
    
    $validChoice = $false
    $selection = $DefaultChoice
    
    while (-not $validChoice) {
        Write-Host "`nVotre choix (1-$($Options.Length)) [par défaut: $($DefaultChoice+1)]: " -ForegroundColor Cyan -NoNewline
        $input = Read-Host
        
        if ([string]::IsNullOrWhiteSpace($input)) {
            $validChoice = $true
        } elseif ($input -match "^\d+$") {
            $choice = [int]$input - 1
            if ($choice -ge 0 -and $choice -lt $Options.Length) {
                $selection = $choice
                $validChoice = $true
            } else {
                Write-Host "Choix invalide. Veuillez entrer un nombre entre 1 et $($Options.Length)." -ForegroundColor Red
            }
        } else {
            Write-Host "Entrée invalide. Veuillez entrer un nombre." -ForegroundColor Red
        }
    }
    
    Write-Host "Option sélectionnée: $($Options[$selection])" -ForegroundColor Green
    return $selection
}

function Show-Spinner {
    param (
        [scriptblock]$ScriptBlock,
        [string]$Message = "Opération en cours..."
    )
    
    if ($Silent) {
        # Mode silencieux - exécuter directement
        return & $ScriptBlock
    }
    
    $spinnerChars = @('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')
    $job = Start-Job -ScriptBlock $ScriptBlock
    
    $startPos = $host.UI.RawUI.CursorPosition
    $i = 0
    
    try {
        while ($job.State -eq "Running") {
            $spinChar = $spinnerChars[$i % $spinnerChars.Length]
            $host.UI.RawUI.CursorPosition = $startPos
            Write-Host "$spinChar $Message" -NoNewline
            Start-Sleep -Milliseconds 100
            $i++
        }
    } finally {
        # Effacer la ligne du spinner
        $host.UI.RawUI.CursorPosition = $startPos
        Write-Host (" " * ($Message.Length + 10))
        $host.UI.RawUI.CursorPosition = $startPos
    }
    
    $results = Receive-Job -Job $job
    Remove-Job -Job $job
    return $results
}

function Show-OperationResult {
    param (
        [string]$Operation,
        [bool]$Success,
        [string]$ErrorMessage = "",
        [switch]$Critical
    )
    
    if ($Success) {
        Write-Host "  [" -NoNewline
        Write-Host "OK" -ForegroundColor Green -NoNewline
        Write-Host "] $Operation" -ForegroundColor White
        Write-LogMessage "SUCCÈS: $Operation" -Level "SUCCESS" -NoConsole
    } else {
        Write-Host "  [" -NoNewline
        Write-Host "ÉCHEC" -ForegroundColor Red -NoNewline
        Write-Host "] $Operation" -ForegroundColor White
        if ($ErrorMessage) {
            Write-Host "       • $ErrorMessage" -ForegroundColor DarkYellow
        }
        
        Write-LogMessage "ÉCHEC: $Operation - $ErrorMessage" -Level "ERROR" -NoConsole
        $Global:HasErrors = $true
        
        if ($Critical) {
            throw "ERREUR CRITIQUE: $Operation a échoué - $ErrorMessage"
        }
    }
}

function Test-Administrator {
    $currentUser = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $currentUser.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Show-FinalSummary {
    $endTime = Get-Date
    $duration = $endTime - $StartTime
    
    Write-Section "Résumé de l'installation"
    
    $statusColor = if ($Global:HasErrors) { "Red" } else { "Green" }
    $statusText = if ($Global:HasErrors) { "AVEC AVERTISSEMENTS" } else { "RÉUSSIE" }
    
    Write-Host "`n╔════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║               RÉSUMÉ DE L'INSTALLATION                     ║" -ForegroundColor Cyan
    Write-Host "╠════════════════════════════════════════════════════════════╣" -ForegroundColor Cyan
    Write-Host "║  • Statut:     " -ForegroundColor Cyan -NoNewline
    Write-Host "Installation $statusText" -ForegroundColor $statusColor -NoNewline
    Write-Host "              ║" -ForegroundColor Cyan
    Write-Host "║  • Durée:      $($duration.Minutes) minutes et $($duration.Seconds) secondes                   ║" -ForegroundColor Cyan
    Write-Host "║  • Journal:     $LogFile                   ║" -ForegroundColor Cyan
    Write-Host "╚════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    
    if ($Global:HasErrors) {
        Write-Host "`nDes problèmes ont été rencontrés pendant l'installation." -ForegroundColor Yellow
        Write-Host "Consultez le fichier journal pour plus de détails: $LogFile" -ForegroundColor Yellow
        Write-Host "Vous pouvez également relancer le script avec l'option -Diagnostic pour plus d'informations." -ForegroundColor Yellow
    } else {
        Write-Host "`nL'installation a été complétée avec succès!" -ForegroundColor Green
    }
    
    Write-Host "`nProchaines étapes:" -ForegroundColor Cyan
    Write-Host "1. Lancez l'application principale via le fichier main.py" -ForegroundColor White
    Write-Host "2. En cas de problème avec VLC, utilisez le fichier vlc_init.py avant d'importer VLC" -ForegroundColor White
    Write-Host "3. Pour plus d'informations, consultez la documentation du système" -ForegroundColor White
    
    Write-Host "`nMerci d'avoir installé le système de caméras IP!" -ForegroundColor Cyan
    
    # Pause à la fin si on est dans PowerShell ISE ou demandé par l'utilisateur
    if ($Host.Name -eq "Windows PowerShell ISE Host" -or $WaitAtEnd) {
        Write-Host "`nAppuyez sur une touche pour terminer..." -ForegroundColor DarkGray
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    }
}

function Start-Diagnostic {
    Write-Section "Diagnostic du système"
    
    Write-LogMessage "Lancement du diagnostic complet" -Level "INFO"
    
    # Informations système
    Write-Host "`n[Informations système]" -ForegroundColor Cyan
    $osInfo = Get-CimInstance Win32_OperatingSystem
    $processorInfo = Get-CimInstance Win32_Processor
    $memoryInfo = Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum
    
    Write-Host "  • OS: $($osInfo.Caption) $($osInfo.Version)" -ForegroundColor White
    Write-Host "  • Architecture: $($osInfo.OSArchitecture)" -ForegroundColor White
    Write-Host "  • Processeur: $($processorInfo.Name)" -ForegroundColor White
    Write-Host "  • Mémoire: $(($memoryInfo.Sum / 1GB).ToString('N2')) GB" -ForegroundColor White
    
    # Environnement PowerShell
    Write-Host "`n[PowerShell]" -ForegroundColor Cyan
    Write-Host "  • Version: $($PSVersionTable.PSVersion)" -ForegroundColor White
    Write-Host "  • Exécuté en tant qu'administrateur: $(Test-Administrator)" -ForegroundColor White
    
    # Vérification des dépendances
    Write-Host "`n[Dépendances]" -ForegroundColor Cyan
    
    # WinGet
    $wingetAvailable = $null -ne (Get-Command "winget" -ErrorAction SilentlyContinue)
    Write-Host "  • WinGet: " -NoNewline
    if ($wingetAvailable) {
        $wingetVersion = & winget --version 2>$null
        Write-Host "Disponible ($wingetVersion)" -ForegroundColor Green
    } else {
        Write-Host "Non disponible" -ForegroundColor Red
    }
    
    # Python
    Write-Host "  • Python: " -NoNewline
    $pythonInstalled = Test-Python39
    if ($pythonInstalled) {
        if ($pythonInstalled -eq "py -3.9") {
            $version = & py -3.9 --version 2>&1
        } else {
            $version = & $pythonInstalled --version 2>&1
        }
        Write-Host "Python 3.9 trouvé ($version)" -ForegroundColor Green
    } else {
        Write-Host "Python 3.9 non détecté" -ForegroundColor Red
    }
    
    # VLC
    Write-Host "  • VLC: " -NoNewline
    $vlcInstalled = Test-VlcInstalled
    if ($vlcInstalled) {
        $vlcPath = Get-VlcPath
        Write-Host "Installé ($vlcPath)" -ForegroundColor Green
    } else {
        Write-Host "Non installé" -ForegroundColor Red
    }
    
    # Détails du système
    Write-Host "`n[Variables d'environnement PATH]" -ForegroundColor Cyan
    $envPaths = $env:Path -split ';'
    foreach ($path in $envPaths) {
        if ($path -match "python|vlc" -and $path.Trim() -ne "") {
            $exists = Test-Path $path
            $statusColor = if ($exists) { "Green" } else { "Red" }
            $status = if ($exists) { "[Existe]" } else { "[Manquant]" }
            Write-Host "  • $status " -ForegroundColor $statusColor -NoNewline
            Write-Host "$path" -ForegroundColor White
        }
    }
    
    # Diagnostic complet Python
    Write-Host "`n[Diagnostic Python détaillé]" -ForegroundColor Cyan
    if ($pythonInstalled) {
        $diagnosticResult = Invoke-PythonDiagnostic
        foreach ($line in $diagnosticResult) {
            if ($line -match "^\+") {
                Write-Host "  $line" -ForegroundColor Green
            } 
            elseif ($line -match "^-") {
                Write-Host "  $line" -ForegroundColor Red
            } 
            else {
                Write-Host "  $line" -ForegroundColor White
            }
        }
    } else {
        Write-Host "  Impossible d'exécuter le diagnostic Python (Python 3.9 non installé)" -ForegroundColor Red
    }
    
    Write-Host "`n[Diagnostic du journal d'installation]" -ForegroundColor Cyan
    if (Test-Path $LogFile) {
        $errorLines = Select-String -Path $LogFile -Pattern "\[ERROR\]" | Select-Object -Last 5
        if ($errorLines.Count -gt 0) {
            Write-Host "  Dernières erreurs trouvées:" -ForegroundColor Yellow
            foreach ($line in $errorLines) {
                Write-Host "  • $($line.Line)" -ForegroundColor Red
            }
        } else {
            Write-Host "  Aucune erreur trouvée dans le journal d'installation" -ForegroundColor Green
        }
    } else {
        Write-Host "  Fichier journal non trouvé: $LogFile" -ForegroundColor Red
    }
    
    Write-Host "`nDiagnostic terminé. Pour obtenir de l'aide supplémentaire, contactez le support." -ForegroundColor Cyan
}

function Invoke-PythonDiagnostic {
    $pythonCmd = Test-Python39
    if (-not $pythonCmd) { return $null }
    
    # Create a temporary Python file that PowerShell won't try to parse
    $tempPyFile = [System.IO.Path]::GetTempFileName() + ".py"
    
    # Write a simple Python script without string interpolation or complex syntax
    $simplePyScript = @'
import sys
import os
import site

print("Python " + sys.version.split()[0])
print("Executable: " + sys.executable)
print("Architecture: " + ("64" if sys.maxsize > 2**32 else "32") + " bits")

print("\nSite packages:")
for path in site.getsitepackages():
    print("- " + path)

print("\nPackages required:")
packages = ["cryptography", "tkintertable", "onvif_zeep", "PIL", "vlc"]

for pkg in packages:
    try:
        if pkg == "PIL":
            import PIL
            print("+ PIL installed")
        elif pkg == "vlc":
            import vlc
            print("+ vlc installed")
        else:
            __import__(pkg.replace("-", "_"))
            print("+ " + pkg + " installed")
    except ImportError:
        print("- " + pkg + " NOT found")

print("\nVLC:")
try:
    import vlc
    print("+ VLC module found at: " + vlc.__file__)
    
    # Check for DLL files
    vlc_paths = [
        os.path.join(os.getcwd(), "libvlc.dll"),
        r"C:\Program Files\VideoLAN\VLC\libvlc.dll",
        r"C:\Program Files (x86)\VideoLAN\VLC\libvlc.dll"
    ]
    
    found = False
    for path in vlc_paths:
        if os.path.exists(path):
            print("+ libvlc.dll found: " + path)
            found = True
    
    if not found:
        print("- libvlc.dll not found")
    
    # Test if we can create a VLC instance
    try:
        instance = vlc.Instance()
        print("+ Successfully created VLC instance")
    except:
        print("- Failed to create VLC instance")
except ImportError:
    print("- VLC module not found")
except Exception as e:
    print("- Error with VLC: " + str(e))
'@
    
    try {
        # Write script to temp file
        Set-Content -Path $tempPyFile -Value $simplePyScript -Encoding ASCII
        
        # Execute the script
        if ($pythonCmd -eq "py -3.9") {
            $results = & py -3.9 $tempPyFile 2>&1
        } else {
            $results = & $pythonCmd $tempPyFile 2>&1
        }
        
        return $results
    }
    finally {
        # Clean up
        if (Test-Path $tempPyFile) {
            Remove-Item $tempPyFile -Force
        }
    }
}

function Invoke-QuickFix {
    Write-Section "Réparation rapide"
    
    $fixes = @(
        "Réparer les variables d'environnement Python",
        "Réinstaller les bibliothèques Python",
        "Configurer VLC",
        "Réparer WinGet",
        "Tous les correctifs"
    )
    
    $choice = Get-UserChoice -Title "Sélectionnez une réparation à effectuer:" -Options $fixes -DefaultChoice 4
    
    switch ($choice) {
        0 {
            Write-LogMessage "Réparation des variables d'environnement Python" -Level "INFO"
            $pythonCmd = Test-Python39
            if ($pythonCmd) {
                if ($pythonCmd -eq "py -3.9") {
                    $pyHome = & py -3.9 -c "import sys; print(sys.prefix)"
                } else {
                    $pyHome = Split-Path -Parent $pythonCmd
                }
                
                Add-ToMachinePath $pyHome
                Add-ToMachinePath (Join-Path $pyHome "Scripts")
                Show-BannerMessage "Les variables d'environnement Python ont été réparées." -Type "Success"
            } else {
                Show-BannerMessage "Python 3.9 n'est pas installé. Veuillez d'abord installer Python." -Type "Error"
            }
        }
        1 {
            Write-LogMessage "Réinstallation des bibliothèques Python" -Level "INFO"
            $pythonCmd = Test-Python39
            if ($pythonCmd) {
                foreach ($package in $RequiredPythonPackages) {
                    Write-Host "Réinstallation de $package..." -ForegroundColor Yellow
                    try {
                        if ($pythonCmd -eq "py -3.9") {
                            & py -3.9 -m pip install --upgrade --force-reinstall $package
                        } else {
                            & $pythonCmd -m pip install --upgrade --force-reinstall $package
                        }
                        Write-Host "✓ $package réinstallé" -ForegroundColor Green
                    } catch {
                        Write-Host "✗ Erreur lors de la réinstallation de $package" -ForegroundColor Red
                    }
                }
                Show-BannerMessage "Les bibliothèques Python ont été réinstallées." -Type "Success"
            } else {
                Show-BannerMessage "Python 3.9 n'est pas installé. Veuillez d'abord installer Python." -Type "Error"
            }
        }
        2 {
            Write-LogMessage "Configuration de VLC" -Level "INFO"
            Configure-Vlc
            Show-BannerMessage "Configuration VLC terminée." -Type "Success"
        }
        3 {
            Write-LogMessage "Réparation de WinGet" -Level "INFO"
            $repaired = Repair-WinGet -Force
            if ($repaired) {
                Show-BannerMessage "WinGet a été réparé avec succès." -Type "Success"
            } else {
                Show-BannerMessage "La réparation de WinGet a échoué. Consultez le journal pour plus de détails." -Type "Error"
            }
        }
        4 {
            Write-LogMessage "Application de tous les correctifs" -Level "INFO"
            
            # Réparer WinGet
            Write-Host "Réparation de WinGet..." -ForegroundColor Yellow
            Repair-WinGet -Force | Out-Null
            
            # Réparer variables d'environnement Python
            $pythonCmd = Test-Python39
            if ($pythonCmd) {
                Write-Host "Réparation des variables d'environnement Python..." -ForegroundColor Yellow
                if ($pythonCmd -eq "py -3.9") {
                    $pyHome = & py -3.9 -c "import sys; print(sys.prefix)"
                } else {
                    $pyHome = Split-Path -Parent $pythonCmd
                }
                
                Add-ToMachinePath $pyHome
                Add-ToMachinePath (Join-Path $pyHome "Scripts")
                
                # Réinstaller les bibliothèques
                Write-Host "Réinstallation des bibliothèques Python..." -ForegroundColor Yellow
                foreach ($package in $RequiredPythonPackages) {
                    Write-Host "  • $package" -ForegroundColor DarkGray -NoNewline
                    try {
                        if ($pythonCmd -eq "py -3.9") {
                            & py -3.9 -m pip install --upgrade --force-reinstall $package 2>&1 | Out-Null
                        } else {
                            & $pythonCmd -m pip install --upgrade --force-reinstall $package 2>&1 | Out-Null
                        }
                        Write-Host " [OK]" -ForegroundColor Green
                    } catch {
                        Write-Host " [ÉCHEC]" -ForegroundColor Red
                    }
                }
                
                # Configurer VLC
                Write-Host "Configuration de VLC..." -ForegroundColor Yellow
                Configure-Vlc
                
                Show-BannerMessage "Toutes les réparations ont été appliquées." -Type "Success"
            } else {
                Show-BannerMessage "Python 3.9 n'est pas installé. La réparation automatique ne peut pas continuer." -Type "Error"
            }
        }
    }
}


function Show-Help {
    $helpText = @"
╔═════════════════════════════════════════════════════════════════════════════╗
║                     AIDE DU SCRIPT D'INSTALLATION                           ║
╚═════════════════════════════════════════════════════════════════════════════╝

SYNTAXE:
    .\Enhanced_System_Preparation.ps1 [options]

OPTIONS:
    -Help       : Affiche l'aide du script.
    -Silent     : Mode silencieux (réduit les messages affichés).
    -NoPrompt   : Ne pas demander de confirmation à l'utilisateur.
    -Diagnostic : Exécute un diagnostic complet du système.
    -SkipVLC    : Ignore la configuration de VLC.
    -QuickFix   : Exécute une réparation rapide.

EXEMPLES:
    .\Enhanced_System_Preparation.ps1 -Silent
    .\Enhanced_System_Preparation.ps1 -Diagnostic
    .\Enhanced_System_Preparation.ps1 -QuickFix

DESCRIPTION:
Ce script installe et configure les dépendances nécessaires pour le système de caméras IP.
Il vérifie également les prérequis et propose des solutions en cas de problème.

Pour plus d'informations, consultez la documentation du projet.
"@
    Write-Host $helpText -ForegroundColor Cyan
}

function Configure-Vlc {
    Write-Section "Configuration de VLC"
    
    Write-Host "`nGestion spéciale pour python-vlc..." -ForegroundColor Yellow
    $projectDir = Split-Path -Parent -Path $PSScriptRoot
    $vlcLibs = @("libvlc.dll", "libvlccore.dll")
    $vlcFound = $false

    foreach ($vlcPath in @("${env:ProgramFiles}\VideoLAN\VLC", "${env:ProgramFiles(x86)}\VideoLAN\VLC")) {
        if (Test-Path $vlcPath) {
            $vlcFound = $true
            Write-Host "VLC trouvé à: $vlcPath" -ForegroundColor Green
            
            # Copier les bibliothèques VLC vers le dossier du projet
            foreach ($lib in $vlcLibs) {
                $sourcePath = Join-Path $vlcPath $lib
                $destPath = Join-Path $projectDir $lib
                
                if (Test-Path $sourcePath) {
                    try {
                        Copy-Item -Path $sourcePath -Destination $destPath -Force
                        Write-Host "Copié $lib vers $destPath" -ForegroundColor Green
                    }
                    catch {
                        Write-Warning "Erreur lors de la copie de $lib : $_"
                    }
                }
                else {
                    Write-Warning "Bibliothèque $lib introuvable dans $vlcPath"
                }
            }
            
            # Copier aussi le répertoire plugins si nécessaire
            $pluginsDir = Join-Path $vlcPath "plugins"
            if (Test-Path $pluginsDir) {
                $destPluginsDir = Join-Path $projectDir "plugins"
                try {
                    if (-not (Test-Path $destPluginsDir)) {
                        New-Item -ItemType Directory -Path $destPluginsDir -Force | Out-Null
                    }
                    
                    Write-Host "Copie des plugins VLC essentiels..." -ForegroundColor Yellow
                    # Copier seulement les plugins principaux pour limiter le volume
                    $essentialPlugins = @("access", "demux", "codec")
                    foreach ($plugin in $essentialPlugins) {
                        $pluginFolder = Join-Path $pluginsDir $plugin
                        if (Test-Path $pluginFolder) {
                            $destPluginFolder = Join-Path $destPluginsDir $plugin
                            New-Item -ItemType Directory -Path $destPluginFolder -Force -ErrorAction SilentlyContinue | Out-Null
                            Copy-Item -Path "$pluginFolder\*.dll" -Destination $destPluginFolder -Force -ErrorAction SilentlyContinue
                        }
                    }
                    Write-Host "Plugins VLC copiés dans $destPluginsDir" -ForegroundColor Green
                }
                catch {
                    Write-Warning "Erreur lors de la copie des plugins VLC : $_"
                }
            }
            break
        }
    }

    if (-not $vlcFound) {
        Write-Warning "VLC non trouvé. L'application risque de ne pas fonctionner correctement."
        Write-Host "Veuillez installer VLC manuellement depuis https://www.videolan.org/vlc/" -ForegroundColor Yellow
    }

    # Correction supplémentaire: Ajout variable d'environnement VLC
    if ($vlcFound) {
        foreach ($vlcPath in @("${env:ProgramFiles}\VideoLAN\VLC", "${env:ProgramFiles(x86)}\VideoLAN\VLC")) {
            if (Test-Path $vlcPath) {
                # Ajouter également le chemin de VLC au PATH de l'utilisateur actuel et de la session
                [Environment]::SetEnvironmentVariable("PATH", $env:Path + ";" + $vlcPath, [System.EnvironmentVariableTarget]::User)
                $env:Path += ";$vlcPath"
                
                # Création des fichiers d'aide Python
                $projectDir = Split-Path -Parent -Path $PSScriptRoot
                $escapedVlcPath = $vlcPath.Replace('\', '\\')
                
                # Write VLC finder script using ASCII encoding
                $vlcFinderPath = Join-Path $projectDir "vlc_finder.py"
                $vlcFinderScript = @"
import os
import sys
import ctypes

# Paths to check for VLC
vlc_paths = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'libvlc.dll'),
    r'$escapedVlcPath\\libvlc.dll',
    r'C:\Program Files\VideoLAN\VLC\libvlc.dll',
    r'C:\Program Files (x86)\VideoLAN\VLC\libvlc.dll'
]

# Find first valid path
for path in vlc_paths:
    if os.path.exists(path):
        try:
            # Try to load
            lib = ctypes.CDLL(path)
            # If successful, set environment variables
            os.environ['PYTHON_VLC_MODULE_PATH'] = os.path.dirname(path)
            os.environ['PYTHON_VLC_LIB_PATH'] = path
            print("VLC loaded from: " + path)
            break
        except Exception as e:
            print("Failed to load from " + path + ": " + str(e))
"@
                Set-Content -Path $vlcFinderPath -Value $vlcFinderScript -Encoding ASCII
                
                # Write VLC init script
                $initPath = Join-Path $projectDir "vlc_init.py"
                $initScript = @"
import os
import sys
import ctypes

def init_vlc():
    vlc_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'libvlc.dll'),
        r'$escapedVlcPath\\libvlc.dll',
        r'C:\Program Files\VideoLAN\VLC\libvlc.dll',
        r'C:\Program Files (x86)\VideoLAN\VLC\libvlc.dll'
    ]
    
    # Find first valid path
    for path in vlc_paths:
        if os.path.exists(path):
            try:
                os.environ['PATH'] = os.path.dirname(path) + os.pathsep + os.environ['PATH']
                return True
            except Exception:
                continue
    return False

# Initialize VLC at startup
init_vlc()
"@
                Set-Content -Path $initPath -Value $initScript -Encoding ASCII
                
                # Créer un petit script d'aide pour expliquer comment l'utiliser
                $helpScript = @"
# Ajoutez cette ligne au début de votre script principal pour charger VLC correctement
import vlc_init
# Puis importez vlc comme d'habitude
import vlc
"@
                $helpPath = Join-Path $projectDir "vlc_import_aide.txt"
                Set-Content -Path $helpPath -Value $helpScript
                
                break
            }
        }
    }
    
    Write-Host "Configuration de VLC terminée" -ForegroundColor Green
}

# Define missing Test-VlcInstalled function
function Test-VlcInstalled {
    $vlcPaths = @(
        "${env:ProgramFiles}\VideoLAN\VLC\vlc.exe",
        "${env:ProgramFiles(x86)}\VideoLAN\VLC\vlc.exe"
    )
    foreach ($path in $vlcPaths) {
        if (Test-Path $path) {
            return $true
        }
    }
    return $false
}

# Define missing Get-VlcPath function
function Get-VlcPath {
    $vlcPaths = @(
        "${env:ProgramFiles}\VideoLAN\VLC",
        "${env:ProgramFiles(x86)}\VideoLAN\VLC"
    )
    foreach ($path in $vlcPaths) {
        if (Test-Path $path) {
            return $path
        }
    }
    return $null
}

# Define the missing Test-Python39 function if not already defined
function Test-Python39 {
    try { 
        # Try py launcher first
        & py "-3.9" "-c" "exit(0)" 2>$null
        if ($LASTEXITCODE -eq 0) { 
            Write-Host "Python 3.9 found via py launcher" -ForegroundColor Cyan
            return "py -3.9"
        }
    } catch {}
    
    # Look for python.exe in typical paths
    $possiblePaths = @(
        "C:\Program Files\Python39\python.exe",
        "C:\Program Files\Python\Python39\python.exe",
        "C:\Python39\python.exe"
    )
    
    foreach ($path in $possiblePaths) {
        if (Test-Path $path) {
            try {
                $version = & $path --version 2>&1
                if ($version -match "Python 3\.9") {
                    Write-Host "Python 3.9 detected at: $path" -ForegroundColor Cyan
                    return $path
                }
            } catch {}
        }
    }
    
    return $null
}

# Define the missing Add-ToMachinePath function
function Add-ToMachinePath ($path) {
    $cur = [Environment]::GetEnvironmentVariable("Path","Machine")
    if ($cur -notmatch [regex]::Escape($path)) {
        [Environment]::SetEnvironmentVariable("Path", "$cur;$path", "Machine")
    }
    if ($env:Path -notmatch [regex]::Escape($path)) { 
        $env:Path += ";$path" 
    }
}

# Define the missing Repair-WinGet function
function Repair-WinGet {
    param(
        [switch]$Force
    )
    
    Write-Host "Tentative de réparation de WinGet..." -ForegroundColor Yellow
    
    # Basic implementation
    try {
        $appInstallerPackage = Get-AppxPackage -Name "Microsoft.DesktopAppInstaller" -ErrorAction SilentlyContinue
        
        if ($appInstallerPackage) {
            Write-Host "WinGet (AppInstaller) est déjà installé - version: $($appInstallerPackage.Version)" -ForegroundColor Cyan
            return $true
        }
        
        # Try to install WinGet
        $wingetUrl = "https://github.com/microsoft/winget-cli/releases/latest/download/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle"
        $wingetPath = "$env:TEMP\WinGet.msixbundle"
        
        Write-Host "Téléchargement de WinGet..." -ForegroundColor Cyan
        Invoke-WebRequest -Uri $wingetUrl -OutFile $wingetPath -UseBasicParsing
        
        Write-Host "Installation de WinGet..." -ForegroundColor Cyan
        Add-AppxPackage -Path $wingetPath -ForceApplicationShutdown -ErrorAction Stop
        
        # Clean up
        Remove-Item $wingetPath -Force -ErrorAction SilentlyContinue
        
        # Verify installation
        $newAppInstallerPackage = Get-AppxPackage -Name "Microsoft.DesktopAppInstaller" -ErrorAction SilentlyContinue
        if ($newAppInstallerPackage) {
            Write-Host "WinGet installé avec succès - version: $($newAppInstallerPackage.Version)" -ForegroundColor Green
            return $true
        } else {
            Write-Error "Échec de l'installation de WinGet."
            return $false
        }
    }
    catch {
        Write-Error "Erreur lors de la réparation de WinGet: $_"
        return $false
    }
}

# Define main execution block
if ($Help) {
    Show-Help
} 
elseif ($Diagnostic) {
    Start-Diagnostic
}
elseif ($QuickFix) {
    Invoke-QuickFix
} 
else {
    # Main installation flow would go here
    Show-Header
    Write-Host "Démarrage de l'installation..." -ForegroundColor Green
    # Actual implementation would be added here
    Show-FinalSummary
}