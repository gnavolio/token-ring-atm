"""Formato dei log del nodo (P1-20, P1-I6, P1-I8).

Ogni evento produce **una** riga di testo identica su terminale e su file (il terminale
aggiunge solo i codici colore ANSI)::

    <HH:MM:SS.mmm> [<ATMn>] <marca 3 car.> <CODICE EVENTO su 21 car.> <campi chiave=valore>

Scelte:
- l'elenco degli eventi è **chiuso** (``EVENTS``): un codice non previsto è un errore di
  programmazione e viene rifiutato, così ogni riga prodotta rispetta le regex ufficiali;
- solo ASCII stampabile: nessuna dipendenza dalla codifica della console (cp1252/cp850) o
  delle pipe;
- millisecondi nel timestamp e ``seq`` nei campi: dai 4 file si ricostruisce l'ordine dei
  possessi e si verifica che le sezioni critiche non si sovrappongono (P1-I8);
- marche ``>>>`` / ``===`` / ``<<<`` sulla stessa colonna per il blocco della sezione critica,
  ``!!!`` per gli allarmi.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Final, Mapping, TextIO

from .protocol import is_valid_node_id

MARK_OFFSET: Final[int] = 20          # indice (0-based) del primo carattere della marca
EVENT_OFFSET: Final[int] = 24         # indice (0-based) del primo carattere del codice evento
EVENT_COLUMN_WIDTH: Final[int] = 21   # larghezza della colonna del codice evento
FIELDS_OFFSET: Final[int] = 46        # indice (0-based) del primo carattere dei campi
MAX_QUOTED_LEN: Final[int] = 100      # lunghezza massima del contenuto di un valore tra virgolette

STYLE_NORMAL: Final[str] = "normale"
STYLE_SECTION: Final[str] = "sezione"
STYLE_ALARM: Final[str] = "allarme"


@dataclass(frozen=True)
class EventSpec:
    """Specifica di un evento: marca di 3 caratteri, chiavi dei campi in ordine, stile."""

    mark: str
    fields: tuple[str, ...]
    style: str


_N = "   "  # marca degli eventi normali (tre spazi)

# Elenco chiuso dei 24 eventi: per ognuno marca, campi nell'ordine di stampa e stile.
# Un evento non elencato qui non può essere registrato.
EVENTS: Final[Mapping[str, EventSpec]] = {
    "NODO_AVVIATO": EventSpec(_N, ("successore", "porta_successore", "ritardo"), STYLE_NORMAL),
    "SCENARIO_CARICATO": EventSpec(_N, ("transazioni",), STYLE_NORMAL),
    "IN_ASCOLTO": EventSpec(_N, ("porta",), STYLE_NORMAL),
    "CONNESSIONE_ATTESA": EventSpec(
        _N, ("successore", "porta_successore", "tentativo"), STYLE_NORMAL),
    "CONNESSIONE_OK": EventSpec(
        _N, ("successore", "porta_successore", "tentativi"), STYLE_NORMAL),
    "PREDECESSORE_CONNESSO": EventSpec(_N, ("remoto",), STYLE_NORMAL),
    "PREDECESSORE_CHIUSO": EventSpec(_N, ("remoto",), STYLE_NORMAL),
    "SUCCESSORE_PERSO": EventSpec("!!!", ("successore", "porta_successore"), STYLE_ALARM),
    "TOKEN_CREATO": EventSpec(_N, ("seq", "saldo"), STYLE_NORMAL),
    "TOKEN_RICEVUTO": EventSpec(_N, ("seq", "saldo", "da"), STYLE_NORMAL),
    "NESSUNA_TRANSAZIONE": EventSpec(_N, ("seq", "saldo"), STYLE_NORMAL),
    "INIZIO_TRANSAZIONE": EventSpec(">>>", ("seq", "saldo", "op", "importo"), STYLE_SECTION),
    "SALDO_AGGIORNATO": EventSpec(
        "===", ("seq", "saldo", "prima", "variazione"), STYLE_SECTION),
    "TRANSAZIONE_RIFIUTATA": EventSpec("!!!", ("seq", "saldo", "motivo"), STYLE_ALARM),
    "FINE_TRANSAZIONE": EventSpec("<<<", ("seq", "saldo", "esito"), STYLE_SECTION),
    "TOKEN_INOLTRATO": EventSpec(_N, ("seq", "saldo", "a"), STYLE_NORMAL),
    "TOKEN_RITIRATO": EventSpec(_N, ("seq", "saldo", "giri"), STYLE_NORMAL),
    "TOKEN_PERSO": EventSpec("!!!", ("seq", "saldo"), STYLE_ALARM),
    "COMANDO_ACCETTATO": EventSpec(_N, ("op", "importo", "in_coda"), STYLE_NORMAL),
    "COMANDO_NON_VALIDO": EventSpec("!!!", ("motivo", "testo"), STYLE_ALARM),
    "STATO": EventSpec(_N, ("token", "in_coda", "ultimo_seq", "saldo_visto"), STYLE_NORMAL),
    "AIUTO": EventSpec(_N, ("uso",), STYLE_NORMAL),
    "ERRORE": EventSpec("!!!", ("tipo", "dettaglio"), STYLE_ALARM),
    "ARRESTO": EventSpec(_N, ("motivo", "codice"), STYLE_NORMAL),
}

QUOTED_FIELDS: Final[frozenset[str]] = frozenset({"testo", "dettaglio", "uso"})
NODE_COLORS: Final[Mapping[str, str]] = {"ATM1": "96", "ATM2": "92", "ATM3": "93", "ATM4": "95"}
SECTION_SGR: Final[Mapping[str, str]] = {
    "ATM1": "7;96", "ATM2": "7;92", "ATM3": "7;93", "ATM4": "7;95"}
ALARM_SGR: Final[str] = "1;91"

# Un valore stringa resta senza virgolette solo se è composto interamente da questi caratteri.
_BARE_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:+-]+")


def format_timestamp(ts: datetime) -> str:
    """Restituisce ``HH:MM:SS.mmm`` con i millisecondi **troncati** (non arrotondati). Pura."""
    return f"{ts.hour:02d}:{ts.minute:02d}:{ts.second:02d}.{ts.microsecond // 1000:03d}"


def _sanitize(text: str) -> str:
    """Rende un testo libero sicuro per la riga di log (ASCII stampabile, max 100 caratteri)."""
    cleaned = "".join(
        "'" if ch == '"' else (ch if 0x20 <= ord(ch) <= 0x7E else "?") for ch in text)
    if len(cleaned) > MAX_QUOTED_LEN:
        cleaned = cleaned[:MAX_QUOTED_LEN - 3] + "..."
    return cleaned


def format_value(key: str, value: object) -> str:
    """Formatta il valore di un campo per la riga di log. Pura.

    ``bool`` -> ``si``/``no``; ``int`` -> cifre; ``float`` -> al più 3 decimali senza zeri
    finali (``1.5``, ``0``); ``None`` -> ``-``; ``str`` -> tra virgolette se il campo è
    testuale (``QUOTED_FIELDS``) o contiene caratteri diversi da ``[A-Za-z0-9_.:+-]``.
    Solleva ``ValueError`` per altri tipi.
    """
    if isinstance(value, bool):  # prima di int: bool è una sottoclasse di int
        return "si" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if value is None:
        return "-"
    if isinstance(value, str):
        if key in QUOTED_FIELDS or _BARE_VALUE_RE.fullmatch(value) is None:
            return '"' + _sanitize(value) + '"'
        return value
    raise ValueError(f"tipo non ammesso per il campo {key!r}: {type(value).__name__}")


def _check_event_and_node(node_id: str, event: str) -> EventSpec:
    """Restituisce la specifica dell'evento; ``ValueError`` se evento o nodo non sono validi."""
    spec = EVENTS.get(event)
    if spec is None:
        raise ValueError(f"evento di log sconosciuto: {event!r}")
    if not is_valid_node_id(node_id):
        raise ValueError(f"identificativo di nodo non valido: {node_id!r}")
    return spec


def format_log_line(ts: datetime, node_id: str, event: str,
                    fields: Mapping[str, object]) -> str:
    """Costruisce la riga di log (senza colori e senza ``\\n``) nel formato a colonne fisse. Pura.

    I campi vengono stampati nell'ordine di ``EVENTS[event].fields``, qualunque sia l'ordine
    in ``fields``. Solleva ``ValueError`` se l'evento non è nell'elenco chiuso, se il nodo non
    è valido o se le chiavi di ``fields`` non sono esattamente quelle previste.
    """
    spec = _check_event_and_node(node_id, event)
    if set(fields) != set(spec.fields):
        raise ValueError(
            f"campi non validi per {event}: attesi {list(spec.fields)}, ricevuti {sorted(fields)}")
    return (format_timestamp(ts) + " [" + node_id + "] " + spec.mark + " "
            + event.ljust(EVENT_COLUMN_WIDTH) + " "
            + " ".join(f"{key}={format_value(key, fields[key])}" for key in spec.fields))


def colorize(line: str, node_id: str, event: str) -> str:
    """Avvolge la riga nei codici ANSI del suo stile. Pura.

    Normale: colore del nodo; sezione critica: video inverso nel colore del nodo; allarme:
    rosso brillante. Solleva ``ValueError`` per evento o nodo non validi.
    """
    spec = _check_event_and_node(node_id, event)
    if spec.style == STYLE_SECTION:
        sgr = SECTION_SGR[node_id]
    elif spec.style == STYLE_ALARM:
        sgr = ALARM_SGR
    else:
        sgr = NODE_COLORS[node_id]
    return f"\x1b[{sgr}m{line}\x1b[0m"


class NodeLogger:
    """Scrive gli eventi del nodo sul file di log e sul terminale, una riga per evento.

    È condiviso dai tre thread del nodo: un ``threading.Lock`` interno serializza le
    scritture, così le righe non si mescolano mai a metà. Il file viene troncato a ogni
    avvio e svuotato (``flush``) dopo ogni riga: un processo terminato bruscamente lascia nel
    file tutte le righe già registrate.
    """

    def __init__(self, node_id: str, log_path: str | os.PathLike[str], use_color: bool,
                 stream: TextIO | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        """Crea la cartella del log se manca e apre il file in scrittura (troncato).

        ``stream`` None -> ``sys.stdout``; ``clock`` None -> ``datetime.now``. Propaga
        ``OSError`` se il file non si può creare o aprire.
        """
        self._node_id = node_id
        self._use_color = use_color
        self._stream = stream if stream is not None else sys.stdout
        self._clock = clock if clock is not None else datetime.now
        self._lock = threading.Lock()
        self._closed = False
        parent = os.path.dirname(os.path.abspath(log_path))
        os.makedirs(parent, exist_ok=True)
        self._file = open(log_path, "w", encoding="utf-8", newline="\n")

    def _write_locked(self, event: str, fields: Mapping[str, object]) -> str:
        """Scrive la riga su file e stream; va chiamata tenendo ``self._lock``."""
        if self._closed:
            return ""
        line = format_log_line(self._clock(), self._node_id, event, fields)
        # Gli errori sul file si propagano: un log che non si può scrivere è un guasto vero.
        self._file.write(line + "\n")
        self._file.flush()
        if self._stream is not None:
            shown = colorize(line, self._node_id, event) if self._use_color else line
            try:
                self._stream.write(shown + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                # Terminale chiuso o pipe rotta: il file resta la registrazione autorevole.
                pass
        return line

    def log(self, event: str, **fields: object) -> str:
        """Registra un evento e restituisce la riga senza colori ("" se il logger è chiuso)."""
        with self._lock:
            return self._write_locked(event, fields)

    def log_final(self, event: str, **fields: object) -> str:
        """Registra l'ultimo evento (``ARRESTO``) e chiude il file **sotto lo stesso lock**.

        Scrittura e chiusura sono atomiche rispetto agli altri thread: un ``log()`` del
        thread di ascolto o della tastiera che arrivi dopo trova il logger chiuso e viene
        scartato. Per questo ``ARRESTO`` è l'ultima riga del file per costruzione, non per
        una questione di tempi: se la riga venisse scritta prima e il file chiuso dopo,
        nel mezzo il thread di ascolto potrebbe aggiungere ``PREDECESSORE_CHIUSO``. Se il
        logger è già chiuso restituisce "".
        """
        with self._lock:
            try:
                return self._write_locked(event, fields)
            finally:
                self._close_locked()

    def _close_locked(self) -> None:
        """Chiude il file; va chiamata tenendo ``self._lock``. Idempotente."""
        if not self._closed:
            self._closed = True
            self._file.close()

    def close(self) -> None:
        """Chiude il file di log (idempotente); dopo, ogni ``log``/``log_final`` è ignorato."""
        with self._lock:
            self._close_locked()
