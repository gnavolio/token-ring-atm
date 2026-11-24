# Token Ring ATM: transazioni bancarie distribuite con mutua esclusione a Token Ring

Quattro sportelli bancomat (ATM1…ATM4), ciascuno un processo Python separato nel proprio
terminale, operano su un unico conto condiviso. La mutua esclusione sulle transazioni
(depositi e prelievi) è garantita solo dall'algoritmo Token Ring: un unico token circola
sull'anello ATM1 → ATM2 → ATM3 → ATM4 → ATM1 via TCP su `127.0.0.1` e trasporta il saldo del
conto, che non esiste in nessuna memoria condivisa. Ogni nodo conosce solo il proprio
successore e registra a terminale e su file il movimento del token e le sezioni critiche.

La relazione completa (architettura, dimostrazione di correttezza, test, limiti) è il PDF
consegnato, di cui una copia si trova in `docs/`.

## Requisiti

- **Python 3.11 o superiore** (verificato con 3.11.9 su Windows 11 Pro). Controllo:
  `python --version`. Se `python` non viene trovato, usare `py -3.11` al posto di `python`.
- **Nessuna dipendenza**: solo libreria standard, nessun `pip install`, nessuna compilazione.

## Come ottenere il codice

```text
git clone <<LINK_GITHUB>>
cd token-ring-atm
```

Oppure **Code → Download ZIP** ed estrarre l'archivio. Tutti i comandi seguenti si eseguono
dalla cartella del repository, quella che contiene `atm_node.py`.

## Avvio rapido (script)

In PowerShell:

```text
powershell -ExecutionPolicy Bypass -File scripts\avvia_demo.ps1
```

oppure, da cmd o con doppio clic:

```text
scripts\avvia_demo.bat
```

Lo script controlla che le porte 5001–5004 siano libere, poi apre quattro finestre di console
separate (titoli `ATM1`…`ATM4`) nell'ordine ATM4, ATM3, ATM2, ATM1. In testa a ogni finestra
stampa `Comando: python atm_node.py ...` (lo stesso comando della sezione seguente) e poi lo
esegue; la versione PowerShell dispone le finestre a griglia 2×2. Parametri facoltativi:

- `avvia_demo.ps1`: `-Scenario riferimento|rifiuto`, `-Ritardo SECONDI`,
  `-CartellaLog PERCORSO`, `-SenzaDisposizione`, `-AreaX`/`-AreaY`/`-AreaLarghezza`/`-AreaAltezza`;
- `avvia_demo.bat`: `[riferimento|rifiuto] ["cartella log"]`, per esempio
  `scripts\avvia_demo.bat rifiuto`.

Per chiudere in un colpo nodi e finestre aperti dagli script (con `-SoloNodi` le finestre
restano aperte):

```text
powershell -ExecutionPolicy Bypass -File scripts\ferma_demo.ps1
```

## Avvio manuale dei 4 nodi

Aprire quattro terminali (PowerShell o cmd: i comandi sono identici) nella cartella del
repository e lanciare **un comando per terminale**, su una sola riga, in qualsiasi ordine
(consigliato: ATM4, ATM3, ATM2, ATM1).

```text
python atm_node.py --id ATM1 --porta 5001 --id-successore ATM2 --porta-successore 5002 --crea-token --scenario scenari/riferimento/ATM1.txt --ritardo 1.5
```

```text
python atm_node.py --id ATM2 --porta 5002 --id-successore ATM3 --porta-successore 5003 --scenario scenari/riferimento/ATM2.txt --ritardo 1.5
```

```text
python atm_node.py --id ATM3 --porta 5003 --id-successore ATM4 --porta-successore 5004 --scenario scenari/riferimento/ATM3.txt --ritardo 1.5
```

```text
python atm_node.py --id ATM4 --porta 5004 --id-successore ATM1 --porta-successore 5001 --scenario scenari/riferimento/ATM4.txt --ritardo 1.5
```

| Nodo | Porta di ascolto | Successore (porta) | Crea il token |
|---|---|---|---|
| ATM1 | 5001 | ATM2 (5002) | sì |
| ATM2 | 5002 | ATM3 (5003) | no |
| ATM3 | 5003 | ATM4 (5004) | no |
| ATM4 | 5004 | ATM1 (5001) | no |

- Solo ATM1 ha `--crea-token`. Saldo iniziale: 1000.
- Scenario di riferimento: ATM1 nessuna operazione (1000), ATM2 prelievo 200 (800), ATM3
  deposito 100 (900), ATM4 prelievo 500 (400). Poi il token continua a circolare.
- Scenario con prelievo rifiutato: stessi comandi con `scenari/rifiuto/` al posto di
  `scenari/riferimento/`.
- Ogni nodo si riconosce dal titolo della finestra (`ATM1`…`ATM4`), dal prefisso `[ATMn]` di
  ogni riga e dal colore (ATM1 ciano, ATM2 verde, ATM3 giallo, ATM4 magenta).
- Elenco completo delle opzioni: `python atm_node.py --help`.

## Comandi da tastiera

Da digitare nella finestra di un nodo, seguiti da Invio:

| Comando | Forma breve | Effetto |
|---|---|---|
| `deposito N` | `d N` | accoda un deposito, eseguito al prossimo possesso del token |
| `prelievo N` | `p N` | accoda un prelievo, eseguito al prossimo possesso del token |
| `stato` | `s` | mostra token in mano, coda, ultimo `seq`, ultimo saldo visto |
| `aiuto` | `h` oppure `?` | mostra la sintassi |
| `esci` | `q` | arresta il nodo |

Per arrestare un nodo si usa anche **Ctrl+C** (ultima riga `ARRESTO motivo=interruzione
codice=0`).

Quando si chiude l'anello mentre il token circola, il nodo che ha il token in mano (o che
dovrebbe inoltrarlo a un successore già chiuso) scrive `!!! TOKEN_PERSO seq=… saldo=…` prima di
`ARRESTO`. È previsto: il token non viene rigenerato (limite noto) e la riga registra dove e con
quale saldo l'anello si è fermato. Lo stesso succede se si arresta un solo nodo mentre ha il
token: l'anello si ferma e per ripartire si riavviano tutti e quattro i nodi.

## Log

Ogni nodo scrive le stesse righe del terminale in `logs/ATM1.log` … `logs/ATM4.log`, nella
cartella `logs` accanto ad `atm_node.py`. I file vengono svuotati a ogni avvio del nodo. La
cartella `logs/` è esclusa dal repository (`.gitignore`) perché contiene file generati a ogni
esecuzione; i log reali registrati durante il video sono in `docs/log_demo/`.

## Test

```text
python -m unittest discover -s tests -v
```

214 test (unitari, di analisi statica e d'integrazione con 4 processi reali), circa 30 s.
I test usano porte tra 47101 e 47994 e cartelle temporanee: possono girare anche con la demo
aperta.

## Struttura del repository

```text
atm_node.py        punto d'ingresso del nodo
tokenring/         codice: protocollo, transazioni, log, rete, riga di comando
scenari/           transazioni di ogni nodo (riferimento/, rifiuto/)
tests/             test automatici
scripts/           script di avvio della demo
video/             video dimostrativo
docs/              copia della relazione in PDF e log della demo (log_demo/)
```

## Video

Video dimostrativo: <<LINK_VIDEO>>

## Autore

Gianni Navolio, matricola 0322600102, corso di Intelligenza Artificiale Distribuita.
