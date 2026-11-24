<#
Arresto di emergenza della demo Token Ring e chiusura delle sue finestre.

COME SI LANCIA (dalla cartella token-ring-atm):

    powershell -ExecutionPolicy Bypass -File scripts\ferma_demo.ps1

QUANDO USARLO
  Il modo normale di fermare la demo e' Ctrl+C in ogni finestra: il nodo scrive l'ultima riga
  ARRESTO ed esce con codice 0. Questo script serve solo quando le porte 5001-5004 restano
  occupate (per esempio una finestra chiusa male o una prova precedente) oppure per chiudere
  in un colpo le finestre rimaste aperte dopo il Ctrl+C.
  Un nodo terminato da qui viene chiuso dall'esterno: nel suo log NON compare la riga ARRESTO.

COSA CHIUDE (e solo questo, mai "tutti i python")
  1. i processi Python il cui comando e' "atm_node.py" con una porta della demo
     (--porta 5001..5004);
  2. le finestre aperte da avvia_demo.ps1 / avvia_demo.bat, riconosciute dal loro comando
     (cmd.exe che contiene "atm_node.py --id ATM" e il messaggio "nodo terminato").
  Con -SoloNodi chiude i nodi ma lascia aperte le finestre, per leggere i log a schermo.
#>
[CmdletBinding()]
param(
    [switch]$SoloNodi
)

Set-StrictMode -Version 2.0
$PorteDemo = 5001, 5002, 5003, 5004

# Tutti i processi con la loro riga di comando (Win32_Process la espone anche senza privilegi
# di amministratore per i processi dell'utente corrente).
$processi = @(Get-CimInstance Win32_Process)

# --- 1. Nodi della demo: processi Python con comando atm_node.py e --porta 5001..5004.
# --- (Il filtro sul nome esclude cmd.exe e conhost.exe delle finestre, la cui riga di comando
# --- contiene lo stesso testo: quelle si chiudono al punto 2.)
$nodi = @($processi | Where-Object {
    $_.Name -match '^python' -and
    $_.CommandLine -and $_.CommandLine -match 'atm_node\.py' -and
    $_.CommandLine -match '--porta\s+500[1-4]\b'
})
foreach ($p in $nodi) {
    $id = [regex]::Match($p.CommandLine, '--id\s+(\S+)').Groups[1].Value
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Write-Host "Nodo $id (PID $($p.ProcessId)) terminato."
    } catch {
        Write-Host "Nodo $id (PID $($p.ProcessId)): impossibile terminarlo ($($_.Exception.Message))." -ForegroundColor Yellow
    }
}
if ($nodi.Count -eq 0) { Write-Host 'Nessun nodo della demo in esecuzione.' }

# --- 2. Finestre della demo (cmd.exe /k aperto dagli script di avvio).
if (-not $SoloNodi) {
    $finestre = @($processi | Where-Object {
        $_.Name -eq 'cmd.exe' -and $_.CommandLine -and
        $_.CommandLine -match 'atm_node\.py --id ATM' -and $_.CommandLine -match 'nodo terminato'
    })
    foreach ($f in $finestre) {
        try { Stop-Process -Id $f.ProcessId -Force -ErrorAction Stop } catch { }
    }
    Write-Host "Finestre della demo chiuse: $($finestre.Count)."
}

# --- 3. Verifica finale delle porte (un nodo appena terminato le libera subito).
Start-Sleep -Milliseconds 500
$ancora = @(Get-NetTCPConnection -State Listen -LocalPort $PorteDemo -ErrorAction SilentlyContinue)
if ($ancora.Count -eq 0) {
    Write-Host 'Porte 5001-5004 libere.' -ForegroundColor Green
    exit 0
}
foreach ($c in $ancora) {
    $nome = (Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Write-Host "Porta $($c.LocalPort) ancora occupata dal processo $($c.OwningProcess) ($nome): non e' un nodo della demo, non viene toccato." -ForegroundColor Yellow
}
exit 1
