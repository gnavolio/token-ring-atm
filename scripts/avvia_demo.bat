@echo off
rem ===========================================================================
rem Avvio della demo Token Ring da cmd (funziona anche con doppio clic):
rem apre 4 finestre di console separate, una per nodo (ATM1..ATM4).
rem
rem Uso (da qualunque cartella: lo script si sposta da solo in token-ring-atm):
rem     scripts\avvia_demo.bat                        scenario di riferimento
rem     scripts\avvia_demo.bat rifiuto                scenario con prelievo rifiutato
rem     scripts\avvia_demo.bat riferimento "%TEMP%\tokenring_demo"
rem                                                   log fuori dalla cartella del progetto
rem
rem Equivale a scripts\avvia_demo.ps1, ma non dipende dall'ExecutionPolicy di
rem PowerShell. Non richiede privilegi di amministratore.
rem Differenza: le finestre NON vengono disposte a 2x2: si affiancano a mano
rem (clic sul titolo e Win+frecce).
rem
rem Ogni finestra stampa il comando del nodo e poi lo esegue: sono esattamente i
rem comandi della relazione. Il titolo "ATMn" lo imposta il nodo stesso.
rem Perche' conhost.exe: su Windows 11 il terminale predefinito puo' essere
rem Windows Terminal, che aprirebbe i nodi come schede di un'unica finestra;
rem conhost.exe garantisce una finestra di console separata per ogni nodo.
rem cmd.exe /k tiene aperta la finestra dopo l'arresto del nodo (Ctrl+C), cosi'
rem restano leggibili l'ultima riga ARRESTO e il codice di uscita.
rem ===========================================================================
setlocal EnableExtensions DisableDelayedExpansion

rem Cartella del progetto = cartella padre di scripts\ (percorsi relativi allo script).
cd /d "%~dp0.."
if not exist "atm_node.py" (
    echo ERRORE: atm_node.py non trovato in "%CD%".
    goto :errore
)

rem --- Parametri facoltativi: scenario e cartella dei log.
set "SCENARIO=%~1"
if "%SCENARIO%"=="" set "SCENARIO=riferimento"
if /i not "%SCENARIO%"=="riferimento" if /i not "%SCENARIO%"=="rifiuto" (
    echo ERRORE: scenario "%SCENARIO%" non valido: usare riferimento oppure rifiuto.
    goto :errore
)
set "LOGOPT="
if not "%~2"=="" set LOGOPT= --cartella-log "%~2"

rem --- Controllo: Python raggiungibile con il comando "python".
where python >nul 2>nul
if errorlevel 1 (
    echo ERRORE: comando "python" non trovato. Installare Python 3.11 e aggiungerlo al PATH.
    goto :errore
)

rem --- Controllo: file dello scenario presenti.
for %%N in (ATM1 ATM2 ATM3 ATM4) do (
    if not exist "scenari\%SCENARIO%\%%N.txt" (
        echo ERRORE: file di scenario mancante: scenari\%SCENARIO%\%%N.txt
        goto :errore
    )
)

rem --- Controllo: porte 5001-5004 libere (nessun processo in ascolto).
set "OCCUPATE="
for %%P in (5001 5002 5003 5004) do (
    netstat -ano -p TCP | findstr /R /C:":%%P  *[0-9.:]*  *LISTENING" && set "OCCUPATE=1"
)
if defined OCCUPATE (
    echo ERRORE: le porte della demo non sono libere ^(righe sopra: l'ultimo numero e' il PID^).
    echo Nessuna finestra e' stata aperta. Chiudere le finestre di una demo precedente
    echo ^(Ctrl+C in ognuna^) oppure eseguire:
    echo     powershell -ExecutionPolicy Bypass -File scripts\ferma_demo.ps1
    goto :errore
)

rem --- Avvio: ATM4, ATM3, ATM2, ATM1 a circa 1 s l'uno dall'altro (l'ordine e' libero;
rem --- ATM1 crea il token e parte per ultimo). "ping -n 2" attende circa 1 s e, a
rem --- differenza di "timeout", funziona anche con l'input rediretto.
start "ATM4" conhost.exe cmd.exe /v:on /k prompt ATM4$G^& title ATM4^& echo Comando: python atm_node.py --id ATM4 --porta 5004 --id-successore ATM1 --porta-successore 5001 --scenario scenari/%SCENARIO%/ATM4.txt --ritardo 1.5%LOGOPT% ^& python atm_node.py --id ATM4 --porta 5004 --id-successore ATM1 --porta-successore 5001 --scenario scenari/%SCENARIO%/ATM4.txt --ritardo 1.5%LOGOPT% ^& echo.^& echo [ATM4] nodo terminato, codice di uscita !errorlevel!
ping -n 2 127.0.0.1 >nul
start "ATM3" conhost.exe cmd.exe /v:on /k prompt ATM3$G^& title ATM3^& echo Comando: python atm_node.py --id ATM3 --porta 5003 --id-successore ATM4 --porta-successore 5004 --scenario scenari/%SCENARIO%/ATM3.txt --ritardo 1.5%LOGOPT% ^& python atm_node.py --id ATM3 --porta 5003 --id-successore ATM4 --porta-successore 5004 --scenario scenari/%SCENARIO%/ATM3.txt --ritardo 1.5%LOGOPT% ^& echo.^& echo [ATM3] nodo terminato, codice di uscita !errorlevel!
ping -n 2 127.0.0.1 >nul
start "ATM2" conhost.exe cmd.exe /v:on /k prompt ATM2$G^& title ATM2^& echo Comando: python atm_node.py --id ATM2 --porta 5002 --id-successore ATM3 --porta-successore 5003 --scenario scenari/%SCENARIO%/ATM2.txt --ritardo 1.5%LOGOPT% ^& python atm_node.py --id ATM2 --porta 5002 --id-successore ATM3 --porta-successore 5003 --scenario scenari/%SCENARIO%/ATM2.txt --ritardo 1.5%LOGOPT% ^& echo.^& echo [ATM2] nodo terminato, codice di uscita !errorlevel!
ping -n 2 127.0.0.1 >nul
start "ATM1" conhost.exe cmd.exe /v:on /k prompt ATM1$G^& title ATM1^& echo Comando: python atm_node.py --id ATM1 --porta 5001 --id-successore ATM2 --porta-successore 5002 --crea-token --scenario scenari/%SCENARIO%/ATM1.txt --ritardo 1.5%LOGOPT% ^& python atm_node.py --id ATM1 --porta 5001 --id-successore ATM2 --porta-successore 5002 --crea-token --scenario scenari/%SCENARIO%/ATM1.txt --ritardo 1.5%LOGOPT% ^& echo.^& echo [ATM1] nodo terminato, codice di uscita !errorlevel!

echo.
echo Demo avviata: scenario "%SCENARIO%", ritardo 1.5 s, 4 finestre separate ATM1..ATM4.
if defined LOGOPT (echo Log: %~2\ATM1.log ... ATM4.log) else (echo Log: %CD%\logs\ATM1.log ... ATM4.log)
echo Affiancare le finestre: clic sul titolo e Win+frecce.
echo Transazioni da tastiera: clic nella finestra del nodo e digitare, per esempio, "prelievo 300" + Invio.
echo Arresto: Ctrl+C in ogni finestra. I log vengono riscritti a ogni avvio: copiarli prima di rilanciare.
endlocal
exit /b 0

:errore
rem Con il doppio clic la finestra si chiuderebbe subito: "pause" lascia leggere il messaggio.
pause
endlocal
exit /b 1
