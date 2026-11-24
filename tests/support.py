"""Modulo di supporto della suite di test.

Contiene:
- le espressioni regolari ufficiali del formato dei log, scritte qui e NON importate dal codice
  di produzione, cosi' la verifica resta indipendente;
- il parsing delle righe di log e la "sottosequenza del token": le sole righe che riguardano il
  token (creazione, ricezione, transazione, inoltro, ritiro, perdita, arresto);
- il verificatore degli invarianti sui log (elenco sotto);
- un modello di riferimento che calcola la sottosequenza del token attesa per uno scenario che
  non ha log di riferimento in ``golden_logs.py``;
- l'avvio e la terminazione garantita dei processi dei nodi per i test d'integrazione.

Invarianti verificati (il numero compare come prefisso ``inv.N`` nei messaggi d'errore):
 1. formato: ogni riga soddisfa le regex, con marca e codice evento nelle colonne fisse; un
    processo terminato da se' ha ``ARRESTO`` come ultima riga del file, una sola volta;
 2. un solo creatore: un solo ``TOKEN_CREATO``, in ATM1, con seq=0 e il saldo iniziale;
 3. unicita' del token: (a) ogni seq e' ricevuto al piu' una volta; (b) ogni inoltro ha
    esattamente una ricezione nel successore, e viceversa; (c) il saldo non cambia in viaggio;
 4. i seq dei possessi sono strettamente crescenti in ogni log e, globalmente, 0, 1, 2, ...
    senza buchi;
 5. i possessi ordinati per seq seguono l'anello ATM1, ATM2, ATM3, ATM4, ATM1, ...;
 7. ogni possesso contiene al massimo una transazione (oppure NESSUNA_TRANSAZIONE) ed e' chiuso
    da inoltro, ritiro o perdita del token (regole in ``check_possessions``);
 8. le sezioni critiche [INIZIO, FINE] di nodi diversi non si sovrappongono mai nel tempo;
10. dentro la sezione critica il saldo e' coerente (saldo - prima == variazione, segno giusto).
I controlli 6 e 9 (saldi attesi di uno scenario preciso) stanno direttamente nei test.
Il prefisso ``causalita'`` segnala una ricezione registrata prima del relativo inoltro.

Nessun import del pacchetto ``tokenring``.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

# I test non devono lasciare file generati (bytecode) nella cartella del progetto.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent          # cartella token-ring-atm
ATM_NODE = ROOT / "atm_node.py"
PACKAGE_DIR = ROOT / "tokenring"

if str(ROOT) not in sys.path:                           # import di ``tokenring`` nei test unitari
    sys.path.insert(0, str(ROOT))

RING: tuple[str, ...] = ("ATM1", "ATM2", "ATM3", "ATM4")
SUCCESSOR: dict[str, str] = {"ATM1": "ATM2", "ATM2": "ATM3", "ATM3": "ATM4", "ATM4": "ATM1"}

# Porte riservate ai test, diverse da quelle della demo: blocco k = 0..89, ATMn -> 47100 + 10k + n.
TEST_PORT_MIN = 47101
TEST_PORT_MAX = 47994
N_PORT_BLOCKS = 90

# Timeout dei test d'integrazione.
LISTEN_TIMEOUT = 5.0        # attesa della riga IN_ASCOLTO dopo ogni avvio
RUN_DEADLINE = 30.0         # scadenza complessiva di un caso di test con --giri
POLL = 0.05                 # intervallo di polling sui file di log
TEST_DELAY = "0.1"          # --ritardo dei test: pausa breve prima di ogni inoltro

CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0   # nessuna finestra visibile


# =====================================================================================
# REGEX UFFICIALI del formato dei log
# =====================================================================================
# Si applicano alla riga senza colori e senza "\n". LINE_RE vale per ogni riga; poi
# FIELDS_RE[evento] deve corrispondere per intero al gruppo "fields" ed EVENT_MARK[evento]
# deve essere uguale al gruppo "mark". Il lookahead di LINE_RE impone la colonna fissa del
# codice evento.

LINE_RE = re.compile(
    r'^(?P<ts>[0-2]\d:[0-5]\d:[0-5]\d\.\d{3}) '
    r'\[(?P<node>ATM[1-4])\] '
    r'(?P<mark>>>>|===|<<<|!!!|   ) '
    r'(?=[A-Z_ ]{21} [a-z])(?P<event>[A-Z_]+) +'
    r'(?P<fields>[a-z_]+=(?:"[^"]*"|[^\s"]+)(?: [a-z_]+=(?:"[^"]*"|[^\s"]+))*)$'
)

_ID = r'ATM[1-4]'
_N = r'(?:0|[1-9]\d*)'        # intero >= 0 senza zeri iniziali
_P = r'[1-9]\d*'              # intero >= 1
_PORT = r'[1-9]\d{3,4}'       # 1000..99999 (il range reale 1024..65535 lo garantisce la CLI)

EVENT_MARK = {
    "NODO_AVVIATO": "   ", "SCENARIO_CARICATO": "   ", "IN_ASCOLTO": "   ",
    "CONNESSIONE_ATTESA": "   ", "CONNESSIONE_OK": "   ",
    "PREDECESSORE_CONNESSO": "   ", "PREDECESSORE_CHIUSO": "   ",
    "SUCCESSORE_PERSO": "!!!", "TOKEN_CREATO": "   ", "TOKEN_RICEVUTO": "   ",
    "NESSUNA_TRANSAZIONE": "   ", "INIZIO_TRANSAZIONE": ">>>", "SALDO_AGGIORNATO": "===",
    "TRANSAZIONE_RIFIUTATA": "!!!", "FINE_TRANSAZIONE": "<<<", "TOKEN_INOLTRATO": "   ",
    "TOKEN_RITIRATO": "   ", "TOKEN_PERSO": "!!!", "COMANDO_ACCETTATO": "   ",
    "COMANDO_NON_VALIDO": "!!!", "STATO": "   ", "AIUTO": "   ", "ERRORE": "!!!",
    "ARRESTO": "   ",
}

FIELDS_RE = {k: re.compile(v) for k, v in {
    "NODO_AVVIATO":
        rf'successore=(?P<successore>{_ID}) porta_successore=(?P<porta_successore>{_PORT}) '
        rf'ritardo=(?P<ritardo>\d+(?:\.\d{{1,3}})?)',
    "SCENARIO_CARICATO": rf'transazioni=(?P<transazioni>{_N})',
    "IN_ASCOLTO": rf'porta=(?P<porta>{_PORT})',
    "CONNESSIONE_ATTESA":
        rf'successore=(?P<successore>{_ID}) porta_successore=(?P<porta_successore>{_PORT}) '
        rf'tentativo=(?P<tentativo>{_P})',
    "CONNESSIONE_OK":
        rf'successore=(?P<successore>{_ID}) porta_successore=(?P<porta_successore>{_PORT}) '
        rf'tentativi=(?P<tentativi>{_P})',
    "PREDECESSORE_CONNESSO": r'remoto=127\.0\.0\.1:(?P<porta_remota>\d{1,5})',
    "PREDECESSORE_CHIUSO": r'remoto=127\.0\.0\.1:(?P<porta_remota>\d{1,5})',
    "SUCCESSORE_PERSO":
        rf'successore=(?P<successore>{_ID}) porta_successore=(?P<porta_successore>{_PORT})',
    "TOKEN_CREATO": rf'seq=(?P<seq>0) saldo=(?P<saldo>{_N})',
    "TOKEN_RICEVUTO": rf'seq=(?P<seq>{_P}) saldo=(?P<saldo>{_N}) da=(?P<da>{_ID})',
    "NESSUNA_TRANSAZIONE": rf'seq=(?P<seq>{_N}) saldo=(?P<saldo>{_N})',
    "INIZIO_TRANSAZIONE":
        rf'seq=(?P<seq>{_N}) saldo=(?P<saldo>{_N}) op=(?P<op>DEPOSITO|PRELIEVO) '
        rf'importo=(?P<importo>{_P})',
    "SALDO_AGGIORNATO":
        rf'seq=(?P<seq>{_N}) saldo=(?P<saldo>{_N}) prima=(?P<prima>{_N}) '
        rf'variazione=(?P<variazione>[+-]{_P})',
    "TRANSAZIONE_RIFIUTATA":
        rf'seq=(?P<seq>{_N}) saldo=(?P<saldo>{_N}) motivo=(?P<motivo>saldo_insufficiente)',
    "FINE_TRANSAZIONE":
        rf'seq=(?P<seq>{_N}) saldo=(?P<saldo>{_N}) esito=(?P<esito>ACCETTATA|RIFIUTATA)',
    "TOKEN_INOLTRATO": rf'seq=(?P<seq>{_P}) saldo=(?P<saldo>{_N}) a=(?P<a>{_ID})',
    "TOKEN_RITIRATO": rf'seq=(?P<seq>{_P}) saldo=(?P<saldo>{_N}) giri=(?P<giri>{_P})',
    "TOKEN_PERSO": rf'seq=(?P<seq>{_N}) saldo=(?P<saldo>{_N})',
    "COMANDO_ACCETTATO":
        rf'op=(?P<op>DEPOSITO|PRELIEVO) importo=(?P<importo>{_P}) in_coda=(?P<in_coda>{_N})',
    "COMANDO_NON_VALIDO":
        r'motivo=(?P<motivo>comando_sconosciuto|importo_mancante|importo_non_valido|'
        r'troppi_argomenti) testo="(?P<testo>[^"]*)"',
    "STATO":
        rf'token=(?P<token>si|no) in_coda=(?P<in_coda>{_N}) '
        rf'ultimo_seq=(?P<ultimo_seq>{_N}|-) saldo_visto=(?P<saldo_visto>{_N}|-)',
    "AIUTO": r'uso="deposito N \| prelievo N \| stato \| aiuto \| esci"',
    "ERRORE":
        r'tipo=(?P<tipo>porta_occupata|scenario_non_valido|messaggio_malformato|'
        r'destinatario_errato|errore_interno) dettaglio="(?P<dettaglio>[^"]*)"',
    "ARRESTO":
        rf'motivo=(?P<motivo>giri_completati|comando_esci|interruzione|errore) '
        rf'codice=(?P<codice>{_N})',
}.items()}
# =====================================================================================

# Sottosequenza del token: le righe che riguardano il possesso e il movimento del token.
SUBSEQ_EVENTS = frozenset({
    "TOKEN_CREATO", "TOKEN_RICEVUTO", "NESSUNA_TRANSAZIONE", "INIZIO_TRANSAZIONE",
    "SALDO_AGGIORNATO", "TRANSAZIONE_RIFIUTATA", "FINE_TRANSAZIONE", "TOKEN_INOLTRATO",
    "TOKEN_RITIRATO", "TOKEN_PERSO", "ARRESTO",
})
OPENINGS = frozenset({"TOKEN_CREATO", "TOKEN_RICEVUTO"})
BODY_EVENTS = frozenset({"NESSUNA_TRANSAZIONE", "INIZIO_TRANSAZIONE", "SALDO_AGGIORNATO",
                         "TRANSAZIONE_RIFIUTATA", "FINE_TRANSAZIONE"})
# Corpi validi di un possesso: nessuna transazione, transazione accettata, transazione rifiutata.
C1 = ("NESSUNA_TRANSAZIONE",)
C2 = ("INIZIO_TRANSAZIONE", "SALDO_AGGIORNATO", "FINE_TRANSAZIONE")
C3 = ("INIZIO_TRANSAZIONE", "TRANSAZIONE_RIFIUTATA", "FINE_TRANSAZIONE")
VALID_BODIES = (C1, C2, C3)


# =====================================================================================
# Parsing delle righe
# =====================================================================================

@dataclass(frozen=True)
class Entry:
    """Una riga di log riconosciuta (LINE_RE + FIELDS_RE)."""
    node: str
    index: int              # posizione (0-based) nel file completo
    raw: str
    ts_ms: int              # millisecondi dalla mezzanotte
    mark: str
    event: str
    fields_text: str
    fields: Mapping[str, str]

    def i(self, name: str) -> int:
        return int(self.fields[name])

    @property
    def short(self) -> str:
        """Forma breve della riga, senza orario, nodo e marca: ``CODICE_EVENTO campi``."""
        return f"{self.event} {self.fields_text}"


def make_line(ts: str, node: str, event: str, fields: str) -> str:
    """Riga costruita con la formula ufficiale del formato (implementazione indipendente, per i test)."""
    return f"{ts} [{node}] {EVENT_MARK[event]} {event.ljust(21)} {fields}"


def ts_to_ms(ts: str) -> int:
    hh, mm, rest = ts.split(":")
    ss, mmm = rest.split(".")
    return ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(mmm)


def check_line(node: str | None, index: int, line: str) -> tuple[Entry | None, list[str]]:
    """Invariante 1 (formato) per una riga. Restituisce l'Entry (se riconosciuta) e gli errori."""
    errors: list[str] = []
    where = f"{node or '?'} riga {index + 1}"
    bad = [c for c in line if not (0x20 <= ord(c) <= 0x7E)]
    if bad:
        errors.append(f"{where}: caratteri non ASCII stampabili {bad[:5]!r}: {line!r}")
    m = LINE_RE.match(line)
    if m is None:
        errors.append(f"{where}: non soddisfa LINE_RE: {line!r}")
        return None, errors
    event = m["event"]
    if node is not None and m["node"] != node:
        errors.append(f"{where}: prefisso [{m['node']}] nel log di {node}: {line!r}")
    if event not in EVENT_MARK:
        errors.append(f"{where}: evento fuori dall'elenco chiuso {event!r}: {line!r}")
        return None, errors
    if m["mark"] != EVENT_MARK[event]:
        errors.append(f"{where}: marca {m['mark']!r} invece di {EVENT_MARK[event]!r}: {line!r}")
    if line[20:23] != m["mark"]:
        errors.append(f"{where}: marca non nelle colonne 20-22: {line!r}")
    if line[24:45].rstrip() != event or len(line) < 46 or line[45] != " ":
        errors.append(f"{where}: codice evento non nelle colonne 24-44: {line!r}")
    if line[46:] != m["fields"]:
        errors.append(f"{where}: campi non a partire dalla colonna 46: {line!r}")
    if line.endswith(" "):
        errors.append(f"{where}: spazio finale: {line!r}")
    fm = FIELDS_RE[event].fullmatch(m["fields"])
    if fm is None:
        errors.append(f"{where}: campi non conformi a FIELDS_RE[{event}]: {line!r}")
        return None, errors
    entry = Entry(node=m["node"], index=index, raw=line, ts_ms=ts_to_ms(m["ts"]),
                  mark=m["mark"], event=event, fields_text=m["fields"],
                  fields=fm.groupdict())
    return entry, errors


def parse_log(node: str | None, lines: Sequence[str]) -> tuple[list[Entry], list[str]]:
    entries: list[Entry] = []
    errors: list[str] = []
    for idx, line in enumerate(lines):
        entry, errs = check_line(node, idx, line)
        errors.extend(errs)
        if entry is not None:
            entries.append(entry)
    return entries, errors


def token_subsequence(entries: Iterable[Entry]) -> list[Entry]:
    return [e for e in entries if e.event in SUBSEQ_EVENTS]


def token_subsequence_strings(lines: Sequence[str]) -> list[str]:
    """Sottosequenza del token di un log, in forma breve (``CODICE campi``)."""
    entries, _ = parse_log(None, lines)
    return [e.short for e in token_subsequence(entries)]


# =====================================================================================
# Invarianti sui log (elenco nella docstring del modulo)
# =====================================================================================

def check_format(node: str, lines: Sequence[str], self_terminated: bool,
                 raw_text: str | None = None) -> list[str]:
    """Inv. 1 (formato) + "``ARRESTO`` ultima riga" sulle righe COMPLETE del file."""
    _, errors = parse_log(node, lines)
    if raw_text is not None:
        if "\r" in raw_text:
            errors.append(f"{node}: il file di log contiene '\\r' (fine riga deve essere '\\n')")
        if raw_text and not raw_text.endswith("\n"):
            errors.append(f"{node}: l'ultima riga del file non termina con '\\n'")
    if self_terminated:
        if not lines:
            errors.append(f"{node}: log vuoto per un processo terminato da se'")
        else:
            last = LINE_RE.match(lines[-1])
            if last is None or last["event"] != "ARRESTO":
                errors.append(f"{node}: l'ultima riga del file non e' ARRESTO: {lines[-1]!r}")
            n_arresto = sum(1 for ln in lines
                            if (m := LINE_RE.match(ln)) is not None and m["event"] == "ARRESTO")
            if n_arresto != 1:
                errors.append(f"{node}: ARRESTO compare {n_arresto} volte (atteso 1)")
    return errors


def _is_prefix_of_valid_body(names: Sequence[str]) -> bool:
    t = tuple(names)
    return any(t == body[:len(t)] for body in VALID_BODIES)


def _forward_failed(entries: Sequence[Entry], fwd: Entry) -> bool:
    """True se dopo questo ``TOKEN_INOLTRATO`` il primo evento rilevante e' ``SUCCESSORE_PERSO``
    (invio fallito: il token resta al nodo, che lo reinvia o lo perde all'arresto)."""
    relevant = {"SUCCESSORE_PERSO", "TOKEN_INOLTRATO", "TOKEN_RICEVUTO", "TOKEN_CREATO",
                "TOKEN_PERSO", "TOKEN_RITIRATO", "ARRESTO"}
    for e in entries:
        if e.index > fwd.index and e.event in relevant:
            return e.event == "SUCCESSORE_PERSO"
    return False


def _check_possession_balances(node: str, opening: Entry, body: Sequence[Entry],
                               closing: Entry | None) -> list[str]:
    """Coerenza del saldo dentro un possesso (invariante 10): il campo ``saldo`` e' sempre
    il saldo del token in quel momento."""
    errs: list[str] = []
    where = f"{node} possesso seq={opening.i('seq')}"
    current = opening.i("saldo")           # saldo del token in mano
    start_balance = None
    op = amount = None
    for e in body:
        if e.event == "NESSUNA_TRANSAZIONE":
            if e.i("saldo") != current:
                errs.append(f"{where}: NESSUNA_TRANSAZIONE saldo={e.i('saldo')} != {current}")
        elif e.event == "INIZIO_TRANSAZIONE":
            start_balance = e.i("saldo")
            op, amount = e.fields["op"], e.i("importo")
            if start_balance != current:
                errs.append(f"{where}: INIZIO_TRANSAZIONE legge saldo={start_balance}, "
                            f"il token ne porta {current} (passo 1 di P1-12)")
        elif e.event == "SALDO_AGGIORNATO":
            new, prima, var = e.i("saldo"), e.i("prima"), int(e.fields["variazione"])
            if new - prima != var:
                errs.append(f"{where}: SALDO_AGGIORNATO saldo-prima={new - prima} != variazione={var}")
            if start_balance is not None and prima != start_balance:
                errs.append(f"{where}: prima={prima} != saldo di INIZIO_TRANSAZIONE={start_balance}")
            if op == "DEPOSITO" and var != amount:
                errs.append(f"{where}: DEPOSITO di {amount} con variazione {var:+d}")
            if op == "PRELIEVO":
                if var != -amount:
                    errs.append(f"{where}: PRELIEVO di {amount} con variazione {var:+d}")
                if start_balance is not None and amount > start_balance:
                    errs.append(f"{where}: PRELIEVO di {amount} accettato con saldo {start_balance}")
            current = new
        elif e.event == "TRANSAZIONE_RIFIUTATA":
            if e.i("saldo") != current:
                errs.append(f"{where}: TRANSAZIONE_RIFIUTATA saldo={e.i('saldo')} != {current}")
            if op != "PRELIEVO" or (amount is not None and amount <= current):
                errs.append(f"{where}: rifiuto non giustificato (op={op}, importo={amount}, "
                            f"saldo={current})")
        elif e.event == "FINE_TRANSAZIONE":
            if e.i("saldo") != current:
                errs.append(f"{where}: FINE_TRANSAZIONE saldo={e.i('saldo')} != {current}")
            names = [b.event for b in body]
            expected = "RIFIUTATA" if "TRANSAZIONE_RIFIUTATA" in names else "ACCETTATA"
            if e.fields["esito"] != expected:
                errs.append(f"{where}: FINE_TRANSAZIONE esito={e.fields['esito']} atteso {expected}")
    if closing is not None and closing.event in ("TOKEN_INOLTRATO", "TOKEN_RITIRATO", "TOKEN_PERSO"):
        if closing.i("saldo") != current:
            errs.append(f"{where}: {closing.event} saldo={closing.i('saldo')} != saldo del token "
                        f"{current}")
    return errs


def check_possessions(node: str, entries: Sequence[Entry], self_terminated: bool) -> list[str]:
    """Invariante 7 sulla sottosequenza del token di un log.

    - possesso: apertura (TOKEN_CREATO/TOKEN_RICEVUTO, seq=k) fino alla prima chiusura
      (TOKEN_INOLTRATO, TOKEN_RITIRATO, TOKEN_PERSO);
    - TOKEN_INOLTRATO: corpo esattamente C1/C2/C3, seq k+1;
    - TOKEN_RITIRATO: corpo VUOTO, seq k, seguito da ARRESTO motivo=giri_completati;
    - TOKEN_PERSO: corpo prefisso di C1/C2/C3 (anche vuoto o completo), seq k, seguito da ARRESTO;
    - righe del corpo con seq=k (aperture e chiusure escluse);
    - reinvio: TOKEN_INOLTRATO con lo stesso seq del precedente ammesso solo con SUCCESSORE_PERSO
      in mezzo nel log completo;
    - fuori dal possesso: solo ARRESTO, o TOKEN_PERSO seguito subito da ARRESTO;
    - processo terminato dal test: l'ultimo possesso puo' restare aperto con corpo prefisso valido.
    Comprende anche la coerenza del saldo (invariante 10).
    """
    errs: list[str] = []
    sub = token_subsequence(entries)
    opening: Entry | None = None
    body: list[Entry] = []
    last_forward: Entry | None = None
    for pos, e in enumerate(sub):
        nxt = sub[pos + 1] if pos + 1 < len(sub) else None
        where = f"{node} riga {e.index + 1} ({e.short})"
        if opening is None:
            if e.event in OPENINGS:
                opening, body = e, []
            elif e.event == "ARRESTO":
                pass
            elif e.event == "TOKEN_PERSO":
                if nxt is None or nxt.event != "ARRESTO":
                    errs.append(f"{where}: TOKEN_PERSO fuori dal possesso non seguito da ARRESTO")
            elif (e.event == "TOKEN_INOLTRATO" and last_forward is not None
                  and e.i("seq") == last_forward.i("seq")):
                between = [x for x in entries
                           if last_forward.index < x.index < e.index and x.event == "SUCCESSORE_PERSO"]
                if not between:
                    errs.append(f"{where}: reinvio dello stesso seq senza SUCCESSORE_PERSO in mezzo")
                last_forward = e
            else:
                errs.append(f"{where}: evento fuori da un possesso del token")
            continue

        k = opening.i("seq")
        if e.event in BODY_EVENTS:
            body.append(e)
            if e.i("seq") != k:
                errs.append(f"{where}: seq del corpo diverso da quello del possesso ({k})")
        elif e.event == "TOKEN_INOLTRATO":
            names = tuple(b.event for b in body)
            if names not in VALID_BODIES:
                errs.append(f"{where}: corpo del possesso seq={k} non valido {list(names)} "
                            f"(ammessi C1/C2/C3: al massimo una transazione per possesso)")
            if e.i("seq") != k + 1:
                errs.append(f"{where}: TOKEN_INOLTRATO seq={e.i('seq')} atteso {k + 1}")
            errs.extend(_check_possession_balances(node, opening, body, e))
            last_forward = e
            opening = None
        elif e.event == "TOKEN_RITIRATO":
            if body:
                errs.append(f"{where}: TOKEN_RITIRATO con corpo non vuoto "
                            f"{[b.event for b in body]}")
            if e.i("seq") != k:
                errs.append(f"{where}: TOKEN_RITIRATO seq={e.i('seq')} atteso {k}")
            if nxt is None or nxt.event != "ARRESTO" or nxt.fields.get("motivo") != "giri_completati":
                errs.append(f"{where}: TOKEN_RITIRATO non seguito da ARRESTO motivo=giri_completati")
            errs.extend(_check_possession_balances(node, opening, body, e))
            opening = None
        elif e.event == "TOKEN_PERSO":
            names = [b.event for b in body]
            if not _is_prefix_of_valid_body(names):
                errs.append(f"{where}: TOKEN_PERSO dopo un corpo non valido {names}")
            if e.i("seq") != k:
                errs.append(f"{where}: TOKEN_PERSO seq={e.i('seq')} atteso {k}")
            if nxt is None or nxt.event != "ARRESTO":
                errs.append(f"{where}: TOKEN_PERSO non seguito da ARRESTO")
            errs.extend(_check_possession_balances(node, opening, body, e))
            opening = None
        else:  # nuova apertura o ARRESTO dentro un possesso
            errs.append(f"{where}: possesso seq={k} non chiuso prima di {e.event}")
            opening, body = (e, []) if e.event in OPENINGS else (None, [])
    if opening is not None:
        names = [b.event for b in body]
        if self_terminated:
            errs.append(f"{node}: possesso seq={opening.i('seq')} non chiuso in un processo "
                        f"terminato da se' (corpo {names})")
        elif not _is_prefix_of_valid_body(names):
            errs.append(f"{node}: ultimo possesso aperto con corpo non valido {names}")
        errs.extend(_check_possession_balances(node, opening, body, None))
    return errs


def check_cross_logs(entries_by_node: Mapping[str, Sequence[Entry]], *,
                     ring: Sequence[str] = RING, creator: str = "ATM1",
                     initial_balance: int = 1000,
                     with_rounds: bool = True) -> list[str]:
    """Invarianti 2, 3, 4, 5, 8, piu' causalita' (nessuna ricezione prima dell'inoltro) e
    successore dell'anello."""
    errs: list[str] = []
    succ = {ring[i]: ring[(i + 1) % len(ring)] for i in range(len(ring))}
    all_entries = [e for n in entries_by_node for e in entries_by_node[n]]

    # Inv. 2: un solo creatore.
    created = [e for e in all_entries if e.event == "TOKEN_CREATO"]
    if len(created) != 1:
        errs.append(f"inv.2: {len(created)} TOKEN_CREATO in tutti i log (atteso 1): "
                    f"{[c.node + ' ' + c.short for c in created]}")
    for c in created:
        if c.node != creator or c.i("seq") != 0 or c.i("saldo") != initial_balance:
            errs.append(f"inv.2: TOKEN_CREATO inatteso in {c.node}: {c.short} "
                        f"(atteso in {creator} seq=0 saldo={initial_balance})")

    received = [e for e in all_entries if e.event == "TOKEN_RICEVUTO"]
    forwards = [e for n in entries_by_node for e in entries_by_node[n]
                if e.event == "TOKEN_INOLTRATO"
                and not _forward_failed(entries_by_node[n], e)]

    # Inv. 3a: ogni seq in al piu' un TOKEN_RICEVUTO.
    seen: dict[int, Entry] = {}
    for r in received:
        k = r.i("seq")
        if k in seen:
            errs.append(f"inv.3a: seq={k} ricevuto due volte ({seen[k].node}, {r.node}): "
                        "due token nel sistema")
        seen[k] = r

    # Inv. 3b/3c: corrispondenza inoltro <-> ricezione, saldo invariato nel viaggio.
    fwd_by_seq: dict[int, list[Entry]] = {}
    for f in forwards:
        fwd_by_seq.setdefault(f.i("seq"), []).append(f)
    for f in forwards:
        k, x, y = f.i("seq"), f.node, f.fields["a"]
        if y != succ.get(x):
            errs.append(f"inv.3b/P1-09: {x} inoltra seq={k} a {y}, ma il suo successore e' {succ.get(x)}")
        matches = [r for r in received if r.i("seq") == k and r.fields["da"] == x]
        if len(matches) != 1:
            errs.append(f"inv.3b: TOKEN_INOLTRATO seq={k} di {x} ha {len(matches)} ricezioni "
                        f"(attesa 1)")
            continue
        r = matches[0]
        if r.node != y:
            errs.append(f"inv.3b: seq={k} inoltrato da {x} a {y} ma ricevuto da {r.node}")
        if r.i("saldo") != f.i("saldo"):
            errs.append(f"inv.3c: seq={k} inoltrato con saldo={f.i('saldo')} e ricevuto con "
                        f"saldo={r.i('saldo')}")
        if r.ts_ms < f.ts_ms:
            errs.append(f"causalita': seq={k} ricevuto ({r.raw[:12]}) prima di essere inoltrato "
                        f"({f.raw[:12]})")
    for r in received:
        k, y, x = r.i("seq"), r.node, r.fields["da"]
        if not any(f.node == x and f.fields["a"] == y for f in fwd_by_seq.get(k, [])):
            errs.append(f"inv.3b: TOKEN_RICEVUTO seq={k} in {y} da {x} senza TOKEN_INOLTRATO "
                        f"corrispondente nel log di {x}")
    if with_rounds:
        if len(forwards) != len(received):
            errs.append(f"inv.3b: {len(forwards)} inoltri e {len(received)} ricezioni "
                        "(con --giri la corrispondenza e' biunivoca)")

    # Inv. 4: seq crescenti nel log e globalmente senza buchi.
    possessions = [e for e in all_entries if e.event in OPENINGS]
    for n, ents in entries_by_node.items():
        seqs = [e.i("seq") for e in ents if e.event in OPENINGS]
        if any(b <= a for a, b in zip(seqs, seqs[1:])):
            errs.append(f"inv.4: seq dei possessi di {n} non strettamente crescenti: {seqs}")
    by_seq = sorted(possessions, key=lambda e: e.i("seq"))
    seqs = [e.i("seq") for e in by_seq]
    if seqs != list(range(len(seqs))):
        errs.append(f"inv.4: seq dei possessi ordinati {seqs} (atteso 0..{len(seqs) - 1} senza buchi)")
    # Storia globale ordinata per tempo: seq strettamente crescenti (P1-14).
    by_time = sorted(possessions, key=lambda e: (e.ts_ms, e.i("seq")))
    tseqs = [e.i("seq") for e in by_time]
    if any(b <= a for a, b in zip(tseqs, tseqs[1:])):
        errs.append(f"P1-14: nella storia globale ordinata per tempo i seq non sono strettamente "
                    f"crescenti: {tseqs}")

    # Inv. 5: ordine dell'anello.
    start = list(ring).index(creator) if creator in ring else 0
    for e in by_seq:
        expected = ring[(start + e.i("seq")) % len(ring)]
        if e.node != expected:
            errs.append(f"inv.5: possesso seq={e.i('seq')} in {e.node}, atteso {expected}")

    # Inv. 8: sezioni critiche disgiunte tra tutti i nodi.
    intervals: list[tuple[int, int, int, str]] = []
    for n, ents in entries_by_node.items():
        open_start: dict[int, Entry] = {}
        for e in ents:
            if e.event == "INIZIO_TRANSAZIONE":
                open_start[e.i("seq")] = e
            elif e.event == "FINE_TRANSAZIONE" and e.i("seq") in open_start:
                s = open_start.pop(e.i("seq"))
                intervals.append((e.i("seq"), s.ts_ms, e.ts_ms, n))
                if e.ts_ms < s.ts_ms:
                    errs.append(f"inv.8: {n} seq={e.i('seq')} FINE prima di INIZIO")
    intervals.sort()
    for a, b in zip(intervals, intervals[1:]):
        if a[2] > b[1]:
            errs.append(f"inv.8: sezione critica di {a[3]} seq={a[0]} [{a[1]},{a[2]}] si sovrappone "
                        f"a quella di {b[3]} seq={b[0]} [{b[1]},{b[2]}]")
        if b[1] < a[1]:
            errs.append(f"inv.8: ordinando per seq le sezioni critiche non risultano ordinate per tempo "
                        f"({a[3]} seq={a[0]}, {b[3]} seq={b[0]})")
    by_start = sorted(intervals, key=lambda t: (t[1], t[2]))
    for a, b in zip(by_start, by_start[1:]):
        if a[2] > b[1]:
            errs.append(f"P1-08: intervalli [inizio, fine] sovrapposti: {a[3]} seq={a[0]} e "
                        f"{b[3]} seq={b[0]}")
    return errs


def check_all(logs: Mapping[str, Sequence[str]], self_terminated: Mapping[str, bool], *,
              raw_texts: Mapping[str, str] | None = None, ring: Sequence[str] = RING,
              creator: str = "ATM1", initial_balance: int = 1000,
              with_rounds: bool = True) -> list[str]:
    """Tutti gli invarianti generali (1, 2, 3, 4, 5, 7, 8, 10) sui 4 log."""
    errs: list[str] = []
    entries_by_node: dict[str, list[Entry]] = {}
    for node, lines in logs.items():
        st = self_terminated.get(node, False)
        errs.extend(check_format(node, lines, st,
                                 raw_texts.get(node) if raw_texts else None))
        entries, _ = parse_log(node, lines)
        entries_by_node[node] = entries
        errs.extend(check_possessions(node, entries, st))
    errs.extend(check_cross_logs(entries_by_node, ring=ring, creator=creator,
                                 initial_balance=initial_balance, with_rounds=with_rounds))
    return errs


def forwarded_balances(logs: Mapping[str, Sequence[str]]) -> dict[int, int]:
    """seq -> saldo dei TOKEN_INOLTRATO, per i controlli dei saldi attesi di uno scenario."""
    out: dict[int, int] = {}
    for node, lines in logs.items():
        entries, _ = parse_log(node, lines)
        for e in entries:
            if e.event == "TOKEN_INOLTRATO":
                out[e.i("seq")] = e.i("saldo")
    return out


# =====================================================================================
# Modello di riferimento della sottosequenza del token
# =====================================================================================

def model_token_subsequences(queues: Mapping[str, Sequence[tuple[str, int]]], rounds: int, *,
                             initial_balance: int = 1000,
                             ring: Sequence[str] = RING) -> dict[str, list[str]]:
    """Sottosequenza del token attesa per ogni nodo, con il primo nodo di ``ring`` creatore e
    ``--giri rounds`` su tutti i nodi. ``queues``: transazioni in coda per nodo, in ordine FIFO,
    come coppie (``"DEPOSITO"``/``"PRELIEVO"``, importo).

    Regole del sistema: possesso -> al piu' una transazione -> inoltro con seq+1; il
    non creatore termina dopo il suo N-esimo inoltro, il creatore al N-esimo rientro registra
    TOKEN_RITIRATO (corpo vuoto) e termina.
    """
    assert rounds > 0
    out: dict[str, list[str]] = {n: [] for n in ring}
    pending = {n: list(queues.get(n, ())) for n in ring}
    creator = ring[0]
    receipts = {n: 0 for n in ring}
    sent = {n: 0 for n in ring}
    balance, seq, idx = initial_balance, 0, 0
    out[creator].append(f"TOKEN_CREATO seq=0 saldo={balance}")
    first = True
    while True:
        node = ring[idx]
        if not first:
            prev = ring[(idx - 1) % len(ring)]
            out[node].append(f"TOKEN_RICEVUTO seq={seq} saldo={balance} da={prev}")
            if node == creator:
                receipts[node] += 1
                if receipts[node] == rounds:
                    out[node].append(f"TOKEN_RITIRATO seq={seq} saldo={balance} giri={rounds}")
                    out[node].append("ARRESTO motivo=giri_completati codice=0")
                    break
        first = False
        if pending[node]:
            op, amount = pending[node].pop(0)
            out[node].append(f"INIZIO_TRANSAZIONE seq={seq} saldo={balance} op={op} importo={amount}")
            if op == "DEPOSITO" or amount <= balance:
                delta = amount if op == "DEPOSITO" else -amount
                new = balance + delta
                out[node].append(f"SALDO_AGGIORNATO seq={seq} saldo={new} prima={balance} "
                                 f"variazione={delta:+d}")
                out[node].append(f"FINE_TRANSAZIONE seq={seq} saldo={new} esito=ACCETTATA")
                balance = new
            else:
                out[node].append(f"TRANSAZIONE_RIFIUTATA seq={seq} saldo={balance} "
                                 f"motivo=saldo_insufficiente")
                out[node].append(f"FINE_TRANSAZIONE seq={seq} saldo={balance} esito=RIFIUTATA")
        else:
            out[node].append(f"NESSUNA_TRANSAZIONE seq={seq} saldo={balance}")
        seq += 1
        nxt = ring[(idx + 1) % len(ring)]
        out[node].append(f"TOKEN_INOLTRATO seq={seq} saldo={balance} a={nxt}")
        sent[node] += 1
        if node != creator and sent[node] == rounds:
            out[node].append("ARRESTO motivo=giri_completati codice=0")
        idx = (idx + 1) % len(ring)
    return out


# =====================================================================================
# Porte
# =====================================================================================

def block_ports(k: int) -> dict[str, int]:
    if not 0 <= k < N_PORT_BLOCKS:
        raise ValueError(k)
    return {f"ATM{n}": 47100 + 10 * k + n for n in range(1, 5)}


def port_is_free(port: int) -> bool:
    """Bind di prova su 127.0.0.1 prima dell'uso. Su Windows con SO_EXCLUSIVEADDRUSE, come il nodo."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def port_is_listening(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


_used_blocks: set[int] = set()


def allocate_block() -> tuple[int, dict[str, int]]:
    """Un blocco di 4 porte di test libere, diverso per ogni caso di test."""
    for k in range(N_PORT_BLOCKS):
        if k in _used_blocks:
            continue
        _used_blocks.add(k)
        ports = block_ports(k)
        if all(port_is_free(p) for p in ports.values()):
            return k, ports
    raise RuntimeError("nessun blocco di porte di test libero tra 47101 e 47994")


# =====================================================================================
# Processi
# =====================================================================================

_ALL_PROCS: list[subprocess.Popen] = []


def _kill_leftovers() -> None:
    """Rete di sicurezza: alla chiusura dell'interprete nessun nodo avviato dai test resta vivo."""
    for p in _ALL_PROCS:
        if p.poll() is None:
            try:
                p.kill()
                p.wait(5)
            except Exception:
                pass


atexit.register(_kill_leftovers)


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # niente __pycache__ nella cartella del progetto
    env.pop("PYTHONSTARTUP", None)
    return env


def popen_python(args: Sequence[str], *, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, *args], cwd=str(ROOT), stdin=stdin, stdout=stdout,
                            stderr=stderr, env=child_env(), creationflags=CREATIONFLAGS)
    _ALL_PROCS.append(proc)
    return proc


def run_python(args: Sequence[str], timeout: float = 20.0,
               input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    """Processo breve (CLI): stdout/stderr catturati, terminato da ``subprocess.run`` se scade."""
    return subprocess.run([sys.executable, *args], cwd=str(ROOT),
                          input=input_bytes if input_bytes is not None else None,
                          stdin=None if input_bytes is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                          env=child_env(), creationflags=CREATIONFLAGS)


def node_args(node_id: str, port: int, succ_id: str, succ_port: int, log_dir: Path, *,
              scenario: str | Path | None = None, create_token: bool = False,
              delay: str = TEST_DELAY, rounds: int | None = None,
              extra: Sequence[str] = ()) -> list[str]:
    """Riga di comando di un nodo nei test (lista di argomenti, mai shell=True)."""
    args = ["atm_node.py", "--id", node_id, "--porta", str(port),
            "--id-successore", succ_id, "--porta-successore", str(succ_port)]
    if create_token:
        args.append("--crea-token")
    if scenario is not None:
        args += ["--scenario", str(scenario)]
    args += ["--ritardo", delay]
    if rounds is not None:
        args += ["--giri", str(rounds)]
    args += ["--no-colori", "--cartella-log", str(log_dir), *extra]
    return args


@dataclass
class NodeProcess:
    node_id: str
    proc: subprocess.Popen
    log_path: Path
    stderr_path: Path
    stderr_file: object
    stopped_by_test: bool = False

    def stop(self) -> None:
        """terminate() -> wait(5) -> kill() -> wait(5), solo per i processi avviati dal test. Idempotente."""
        if self.proc.poll() is None:
            self.stopped_by_test = True
            try:
                self.proc.terminate()
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.stderr_file.close()
        except Exception:
            pass

    @property
    def self_terminated(self) -> bool:
        return self.proc.returncode is not None and not self.stopped_by_test

    def stderr_text(self) -> str:
        try:
            return self.stderr_path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return ""


def read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_complete_lines(path: Path) -> list[str]:
    """Righe complete (terminate da ``\\n``) del file di log; quella in scrittura e' ignorata."""
    text = read_text(path)
    return text.split("\n")[:-1]


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = POLL) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def find_line(path: Path, pattern: str) -> str | None:
    rx = re.compile(pattern)
    for line in read_complete_lines(path):
        if rx.search(line):
            return line
    return None


def wait_log(path: Path, pattern: str, timeout: float) -> str | None:
    found: list[str] = []

    def pred() -> bool:
        line = find_line(path, pattern)
        if line is not None:
            found.append(line)
            return True
        return False

    return found[0] if wait_until(pred, timeout) else None


class Harness:
    """Cartella temporanea + processi di un caso di test. ``close()`` termina SEMPRE tutto."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tokenring_"))   # %TEMP%, fuori da OneDrive
        self.log_dir = self.tmp / "logs"
        self.nodes: dict[str, NodeProcess] = {}
        self.order: list[str] = []
        self._closers: list[Callable[[], None]] = []
        self._closed = False

    def add_closer(self, fn: Callable[[], None]) -> None:
        self._closers.append(fn)

    def start_node(self, node_id: str, port: int, succ_id: str, succ_port: int, *,
                   stdin=subprocess.DEVNULL, **kwargs) -> NodeProcess:
        stderr_path = self.tmp / f"stderr_{node_id}.txt"
        stderr_file = open(stderr_path, "wb")
        try:
            proc = popen_python(node_args(node_id, port, succ_id, succ_port, self.log_dir, **kwargs),
                                stdin=stdin, stdout=subprocess.DEVNULL, stderr=stderr_file)
        except BaseException:
            stderr_file.close()
            raise
        np_ = NodeProcess(node_id, proc, self.log_dir / f"{node_id}.log", stderr_path, stderr_file)
        self.nodes[node_id] = np_
        self.order.append(node_id)
        return np_

    def wait_listening(self, node_id: str, timeout: float = LISTEN_TIMEOUT) -> bool:
        """Attende la riga IN_ASCOLTO (polling ogni 50 ms, al massimo 5 s): cosi' l'ordine di
        avvio voluto dal test e' effettivo."""
        return wait_log(self.nodes[node_id].log_path, r"\] {5}IN_ASCOLTO ", timeout) is not None

    def wait_all_exit(self, deadline: float = RUN_DEADLINE) -> list[str]:
        """Attende la fine di tutti i processi entro una scadenza complessiva. Restituisce i
        nodi ancora vivi alla scadenza (che vengono poi terminati dal test)."""
        end = time.monotonic() + deadline
        alive: list[str] = []
        for nid, np_ in self.nodes.items():
            try:
                np_.proc.wait(timeout=max(0.05, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                alive.append(nid)
        for nid in alive:
            self.nodes[nid].stop()
        return alive

    def lines(self, node_id: str) -> list[str]:
        return read_complete_lines(self.nodes[node_id].log_path)

    def raw(self, node_id: str) -> str:
        return read_text(self.nodes[node_id].log_path)

    def dump(self, last: int = 60) -> str:
        """Estratto dei log e di stderr da allegare ai messaggi d'asserzione."""
        out = [f"--- cartella temporanea: {self.tmp}"]
        for nid, np_ in self.nodes.items():
            lines = read_complete_lines(np_.log_path)
            out.append(f"--- {nid}.log (returncode={np_.proc.poll()}, terminato dal test="
                       f"{np_.stopped_by_test}, {len(lines)} righe, ultime {last}):")
            out.extend("    " + ln for ln in lines[-last:])
            err = np_.stderr_text().strip()
            if err:
                out.append(f"--- stderr {nid}:")
                out.extend("    " + ln for ln in err.splitlines()[-20:])
        return "\n".join(out)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for np_ in self.nodes.values():
            np_.stop()
        for fn in reversed(self._closers):
            try:
                fn()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def require_code() -> None:
    """Fallisce (non salta) se il codice di produzione non c'e'."""
    if not ATM_NODE.is_file():
        raise AssertionError(f"codice assente: {ATM_NODE} non esiste")


def format_errors(errors: Sequence[str], limit: int = 40) -> str:
    shown = list(errors[:limit])
    if len(errors) > limit:
        shown.append(f"... altri {len(errors) - limit} errori")
    return "\n".join("  - " + e for e in shown)
