<#
Avvio della demo Token Ring: apre 4 finestre di console separate, una per nodo (ATM1..ATM4).

COME SI LANCIA (dalla cartella token-ring-atm, Windows PowerShell 5.1 o successivo):

    powershell -ExecutionPolicy Bypass -File scripts\avvia_demo.ps1

  "-ExecutionPolicy Bypass" vale solo per questa esecuzione e non modifica la policy del
  sistema: serve perche' su Windows la policy predefinita blocca gli script .ps1.
  Variante con il profilo utente escluso e percorso esplicito:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\avvia_demo.ps1

  Esempi di parametri (tutti facoltativi):

    ... -File scripts\avvia_demo.ps1 -Scenario rifiuto
    ... -File scripts\avvia_demo.ps1 -CartellaLog "$env:TEMP\tokenring_demo"
    ... -File scripts\avvia_demo.ps1 -SenzaDisposizione

COSA FA
  1. Controlla Python e i file dello scenario, e che le porte 5001-5004 siano libere: se una
     porta e' occupata si ferma con un messaggio, senza aprire nessuna finestra.
  2. Apre 4 finestre in ordine ATM4, ATM3, ATM2, ATM1, a circa 1 s l'una dall'altra.
     L'ordine e' libero (ogni nodo riprova a collegarsi al successore finche' non risponde);
     ATM1, che crea il token, parte per ultimo cosi' si vedono anche i tentativi di
     connessione degli altri nodi.
  3. In ogni finestra stampa il comando e poi lo esegue: sono esattamente i comandi della
     relazione (uno per nodo). Il titolo della finestra "ATMn" lo imposta il nodo stesso.
  4. Dispone le finestre a 2x2 nell'area indicata (predefinita: 1920x1080 in alto a sinistra
     dello schermo principale), in senso orario come l'anello:
         ATM1 (alto sinistra)   -> ATM2 (alto destra)
         ATM4 (basso sinistra) <-  ATM3 (basso destra)

PERCHE' conhost.exe
  Su Windows 11 il terminale predefinito puo' essere Windows Terminal: un normale
  Start-Process aprirebbe i nodi come SCHEDE della stessa finestra. Avviando esplicitamente
  conhost.exe ogni nodo ha sempre la propria finestra di console, con il proprio titolo.
  La finestra usa cmd.exe /k solo per restare aperta dopo l'arresto del nodo, cosi' l'ultima
  riga di log (ARRESTO) e il codice di uscita restano leggibili.

COME SI FERMA
  Ctrl+C in ogni finestra (arresto ordinato, codice di uscita 0), poi si chiudono le finestre.
  In emergenza: powershell -ExecutionPolicy Bypass -File scripts\ferma_demo.ps1

Non servono privilegi di amministratore. Non modifica impostazioni di sistema.
#>
[CmdletBinding()]
param(
    # Cartella dello scenario dentro scenari\ (un file ATMn.txt per nodo).
    [ValidateSet('riferimento', 'rifiuto')]
    [string]$Scenario = 'riferimento',

    # Secondi di pausa prima di ogni inoltro del token (opzione --ritardo del nodo).
    [ValidateRange(0, 60)]
    [double]$Ritardo = 1.5,

    # Facoltativa: cartella dei file di log (opzione --cartella-log). Se vuota il nodo usa la
    # cartella predefinita token-ring-atm\logs e i comandi restano identici alla relazione.
    [string]$CartellaLog = '',

    # Non spostare le finestre (restano dove le apre Windows).
    [switch]$SenzaDisposizione,

    # Area dello schermo (in pixel) in cui disporre le 4 finestre a 2x2.
    [int]$AreaX = 0,
    [int]$AreaY = 0,
    [int]$AreaLarghezza = 1920,
    [int]$AreaAltezza = 1080
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Write-Errore([string]$Messaggio) {
    Write-Host "ERRORE: $Messaggio" -ForegroundColor Red
}

# --- Percorsi: tutto e' relativo alla posizione di questo script (scripts\ -> token-ring-atm\),
# --- cosi' lo script funziona da qualunque cartella e anche se il progetto viene spostato.
$Progetto = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $Progetto 'atm_node.py'))) {
    Write-Errore "atm_node.py non trovato in '$Progetto'. Lo script deve stare in token-ring-atm\scripts\."
    exit 1
}

# --- I 4 nodi: ID, porta, successore. L'anello esiste solo qui, nei comandi di avvio
# --- (il codice del nodo non contiene nessuna tabella dell'anello).
$Nodi = @(
    @{ Id = 'ATM1'; Porta = 5001; IdSucc = 'ATM2'; PortaSucc = 5002; CreaToken = $true  },
    @{ Id = 'ATM2'; Porta = 5002; IdSucc = 'ATM3'; PortaSucc = 5003; CreaToken = $false },
    @{ Id = 'ATM3'; Porta = 5003; IdSucc = 'ATM4'; PortaSucc = 5004; CreaToken = $false },
    @{ Id = 'ATM4'; Porta = 5004; IdSucc = 'ATM1'; PortaSucc = 5001; CreaToken = $false }
)

# --- Controllo 1: Python raggiungibile con il comando "python" (lo stesso della relazione).
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    Write-Errore "comando 'python' non trovato. Installare Python 3.11 e aggiungerlo al PATH."
    exit 1
}

# --- Controllo 2: file dello scenario presenti (il nodo li validerebbe comunque, ma qui
# --- l'errore arriva prima di aprire le finestre).
foreach ($n in $Nodi) {
    $f = Join-Path $Progetto ("scenari\{0}\{1}.txt" -f $Scenario, $n.Id)
    if (-not (Test-Path -LiteralPath $f)) {
        Write-Errore "file di scenario mancante: $f"
        exit 1
    }
}

# --- Controllo 3: porte di ascolto libere. Si cerca chi le occupa (di solito i nodi di una
# --- demo precedente ancora aperta) e si prova un bind, che rileva anche le porte riservate
# --- dal sistema (per esempio da Hyper-V) dove nessun processo e' in ascolto.
$occupate = @()
foreach ($n in $Nodi) {
    $porta = [int]$n.Porta
    $inAscolto = @(Get-NetTCPConnection -State Listen -LocalPort $porta -ErrorAction SilentlyContinue)
    if ($inAscolto.Count -gt 0) {
        $pidProc = $inAscolto[0].OwningProcess
        $nome = (Get-Process -Id $pidProc -ErrorAction SilentlyContinue).ProcessName
        $occupate += "porta $porta in uso dal processo $pidProc ($nome)"
        continue
    }
    $prova = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $porta
    try { $prova.Start() } catch { $occupate += "porta $porta non utilizzabile: $($_.Exception.InnerException.Message)" }
    finally { try { $prova.Stop() } catch { } }
}
if ($occupate.Count -gt 0) {
    Write-Errore 'le porte della demo non sono libere, nessuna finestra e'' stata aperta.'
    $occupate | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    Write-Host 'Chiudere le finestre di una demo precedente (Ctrl+C in ognuna) oppure eseguire:'
    Write-Host '  powershell -ExecutionPolicy Bypass -File scripts\ferma_demo.ps1'
    exit 2
}

# --- Avvisi non bloccanti, utili soprattutto prima di registrare il video.
if ($CartellaLog -eq '' -and $Progetto -match 'OneDrive') {
    Write-Host 'AVVISO: il progetto e'' in una cartella di OneDrive e i log andranno in token-ring-atm\logs.' -ForegroundColor Yellow
    Write-Host '        Per la registrazione usare la copia fuori da OneDrive oppure -CartellaLog "$env:TEMP\tokenring_demo".' -ForegroundColor Yellow
}
try {
    $dpi = (Get-ItemProperty 'HKCU:\Control Panel\Desktop\WindowMetrics' -Name AppliedDPI -ErrorAction Stop).AppliedDPI
    if ($dpi -ne 96) {
        Write-Host "AVVISO: scala dello schermo diversa dal 100% (DPI $dpi): la disposizione 2x2 potrebbe non coincidere con l'area registrata." -ForegroundColor Yellow
    }
} catch { }

# --- Supporto per disporre le finestre (API Win32 via Add-Type, nessuna dipendenza esterna).
# --- Su Windows 10/11 le finestre hanno un bordo invisibile di qualche pixel: con
# --- DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS) si compensa, cosi' i bordi visibili
# --- delle 4 finestre combaciano esattamente con l'area.
if (-not $SenzaDisposizione -and -not ('TokenRingDemo.Finestre' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace TokenRingDemo {
    public static class Finestre {
        [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left, Top, Right, Bottom; }
        [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
        [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int hh, uint flags);
        [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
        public static bool Disponi(IntPtr h, int x, int y, int w, int hh) {
            ShowWindow(h, 9);                                   // SW_RESTORE
            RECT wr, fr;
            if (!GetWindowRect(h, out wr)) return false;
            int l = 0, t = 0, r = 0, b = 0;
            if (DwmGetWindowAttribute(h, 9, out fr, Marshal.SizeOf(typeof(RECT))) == 0) {
                l = fr.Left - wr.Left; t = fr.Top - wr.Top; r = wr.Right - fr.Right; b = wr.Bottom - fr.Bottom;
            }
            return SetWindowPos(h, IntPtr.Zero, x - l, y - t, w + l + r, hh + t + b, 0x0004 | 0x0010); // NOZORDER, NOACTIVATE
        }
    }
}
'@
}

# Posizione di ogni nodo nella griglia 2x2, in senso orario come l'anello.
$Griglia = @{ ATM1 = @(0, 0); ATM2 = @(1, 0); ATM3 = @(1, 1); ATM4 = @(0, 1) }
$larghezzaCella = [int][math]::Floor($AreaLarghezza / 2)
$altezzaCella = [int][math]::Floor($AreaAltezza / 2)

# --- Avvio: ATM4, ATM3, ATM2, ATM1.
$ritardoTesto = $Ritardo.ToString([System.Globalization.CultureInfo]::InvariantCulture)
$avviati = @()
foreach ($n in ($Nodi | Sort-Object { $_.Id } -Descending)) {
    $id = $n.Id
    # Argomenti del nodo, nello stesso ordine dei comandi della relazione.
    $argomenti = @('atm_node.py', '--id', $id, '--porta', $n.Porta,
                   '--id-successore', $n.IdSucc, '--porta-successore', $n.PortaSucc)
    if ($n.CreaToken) { $argomenti += '--crea-token' }
    $argomenti += @('--scenario', "scenari/$Scenario/$id.txt", '--ritardo', $ritardoTesto)
    if ($CartellaLog -ne '') { $argomenti += @('--cartella-log', $CartellaLog) }
    # Un argomento con spazi va tra virgolette (solo il percorso dei log puo' averne).
    $comando = 'python ' + (($argomenti | ForEach-Object {
        $a = [string]$_; if ($a -match '\s') { '"' + $a + '"' } else { $a } }) -join ' ')

    # Riga di comando della finestra: cmd.exe /k resta aperto dopo l'uscita del nodo.
    #   prompt/title: prompt corto e titolo che resta "ATMn" anche dopo l'arresto;
    #   echo: mostra nel video il comando esatto prima di eseguirlo;
    #   /v:on e !errorlevel!: stampa il codice di uscita reale del nodo.
    # Lo spazio prima di "&" dopo il comando serve se il percorso dei log ha spazi: conhost
    # ricompone gli argomenti e un "&" attaccato alle virgolette finirebbe dentro il percorso.
    $rigaCmd = "cmd.exe /v:on /k prompt $id`$G& title $id& echo Comando: $comando & $comando & " +
               "echo.& echo [$id] nodo terminato, codice di uscita !errorlevel!"
    $conhost = Start-Process -FilePath 'conhost.exe' -ArgumentList $rigaCmd `
                             -WorkingDirectory $Progetto -PassThru
    $avviati += [pscustomobject]@{ Id = $id; Conhost = $conhost.Id; Comando = $comando }

    if (-not $SenzaDisposizione) {
        # La finestra appartiene (per Windows) al cmd.exe figlio di conhost: lo si attende per
        # al massimo 5 s e se ne usa l'handle. Si identifica per PID, mai per titolo, cosi'
        # una finestra "ATMn" rimasta aperta da una prova precedente non viene confusa.
        $hwnd = [IntPtr]::Zero
        for ($i = 0; $i -lt 50 -and $hwnd -eq [IntPtr]::Zero; $i++) {
            Start-Sleep -Milliseconds 100
            $figlio = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($conhost.Id) AND Name='cmd.exe'" -ErrorAction SilentlyContinue
            if ($figlio) {
                $p = Get-Process -Id $figlio.ProcessId -ErrorAction SilentlyContinue
                if ($p) { $hwnd = $p.MainWindowHandle }
            }
        }
        if ($hwnd -ne [IntPtr]::Zero) {
            $col, $riga = $Griglia[$id]
            [void][TokenRingDemo.Finestre]::Disponi($hwnd, $AreaX + $col * $larghezzaCella,
                $AreaY + $riga * $altezzaCella, $larghezzaCella, $altezzaCella)
        } else {
            Write-Host "AVVISO: finestra di $id non trovata, disporla a mano (Win+frecce)." -ForegroundColor Yellow
        }
    }
    Start-Sleep -Seconds 1
}

# --- Riepilogo nella console da cui e' stato lanciato lo script.
Write-Host ''
Write-Host "Demo avviata: scenario '$Scenario', ritardo $ritardoTesto s, 4 finestre separate." -ForegroundColor Green
$avviati | Sort-Object Id | ForEach-Object { Write-Host ("  {0}  {1}" -f $_.Id, $_.Comando) }
if ($CartellaLog -ne '') { Write-Host "Log: $CartellaLog\ATM1.log ... ATM4.log" }
else { Write-Host "Log: $(Join-Path $Progetto 'logs')\ATM1.log ... ATM4.log" }
Write-Host 'Transazioni da tastiera: clic nella finestra del nodo e digitare, per esempio, "prelievo 300" + Invio.'
Write-Host 'Arresto: Ctrl+C in ogni finestra. I log vengono riscritti a ogni avvio: copiarli prima di rilanciare.'
exit 0
