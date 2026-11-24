"""Transazioni sul conto e parsing delle richieste (P1-07, P1-12, P1-13, P1-I2, P1-I7).

Contiene:
- ``Transaction`` e ``apply_transaction``: la logica della sezione critica come **funzione
  pura** (validazione e calcolo del nuovo saldo), senza I/O e senza stato;
- il parser delle righe dei file di scenario (``parse_scenario_line``, ``load_scenario``);
- il parser dei comandi da tastiera (``parse_command``).

Le richieste lette da file o da tastiera sono solo **richieste** (operazione e importo):
non contengono mai un saldo. Il saldo esiste solo nel token (P1-I1).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Final

DEPOSIT: Final[str] = "DEPOSITO"
WITHDRAWAL: Final[str] = "PRELIEVO"
MAX_AMOUNT: Final[int] = 1_000_000_000
REASON_INSUFFICIENT_FUNDS: Final[str] = "saldo_insufficiente"

PARSE_REASONS: Final[tuple[str, ...]] = (
    "comando_sconosciuto", "importo_mancante", "importo_non_valido", "troppi_argomenti")

SCENARIO_IO_REASONS: Final[tuple[str, ...]] = ("file_non_leggibile", "codifica_non_valida")

CMD_OPERATION: Final[str] = "operation"
CMD_STATUS: Final[str] = "status"
CMD_HELP: Final[str] = "help"
CMD_QUIT: Final[str] = "quit"

# Parole ammesse (già in minuscolo) per ogni operazione e comando, alias compresi.
_OPERATION_WORDS: Final[dict[str, str]] = {
    "deposito": DEPOSIT, "d": DEPOSIT,
    "prelievo": WITHDRAWAL, "p": WITHDRAWAL,
}
_STATUS_WORDS: Final[frozenset[str]] = frozenset({"stato", "s"})
_HELP_WORDS: Final[frozenset[str]] = frozenset({"aiuto", "h", "?"})
_QUIT_WORDS: Final[frozenset[str]] = frozenset({"esci", "q"})

# Importo: solo cifre ASCII, al massimo 10 (gli zeri iniziali sono ammessi: "007" vale 7).
_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]{1,10}")


def _is_plain_int(value: object) -> bool:
    """True se ``value`` è un ``int`` e non un ``bool``."""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Transaction:
    """Una richiesta di transazione: ``op`` (DEPOSITO/PRELIEVO) e ``amount`` (1..MAX_AMOUNT).

    Il costruttore valida i campi, quindi una ``Transaction`` con importo non valido non può
    esistere: la sezione critica non deve mai validare l'importo, solo il saldo.
    """

    op: str
    amount: int

    def __post_init__(self) -> None:
        """Valida ``op`` e ``amount``; solleva ``ValueError`` se non sono ammessi."""
        if self.op not in (DEPOSIT, WITHDRAWAL):
            raise ValueError(f"operazione non valida: {self.op!r}")
        if not _is_plain_int(self.amount) or not 1 <= self.amount <= MAX_AMOUNT:
            raise ValueError(f"importo non valido: {self.amount!r} (atteso 1..{MAX_AMOUNT})")


@dataclass(frozen=True)
class TransactionResult:
    """Esito di ``apply_transaction``.

    ``new_balance == old_balance`` e ``reason == REASON_INSUFFICIENT_FUNDS`` se la
    transazione è stata rifiutata; ``reason is None`` se è stata accettata.
    """

    accepted: bool
    old_balance: int
    new_balance: int
    reason: str | None


def apply_transaction(balance: int, tx: Transaction) -> TransactionResult:
    """Valida la transazione sul saldo letto dal token e calcola il nuovo saldo (passi 2-3 di P1-12).

    Logica della sezione critica, isolata in una **funzione pura**: nessun I/O, nessuno
    stato, argomenti non modificati. Per questo è verificabile da sola e non può leggere o
    scrivere il saldo da nessun'altra parte che il parametro ricevuto (il saldo del token).

    Regole:
    - ``DEPOSITO``: sempre accettato, nuovo saldo = saldo + importo;
    - ``PRELIEVO``: accettato solo se ``importo <= saldo`` (prelevare l'intero saldo è
      ammesso e porta a 0). Altrimenti è **rifiutato** (P1-I2) e il saldo resta invariato:
      il conto non può andare in negativo e un rifiuto non deve lasciare tracce sul saldo,
      così il token inoltrato porta esattamente il saldo ricevuto.

    Solleva ``ValueError`` se ``balance`` non è un intero (non ``bool``) >= 0.
    """
    if not _is_plain_int(balance) or balance < 0:
        raise ValueError(f"saldo non valido: {balance!r} (atteso intero >= 0)")
    if tx.op == DEPOSIT:
        return TransactionResult(True, balance, balance + tx.amount, None)
    if tx.amount <= balance:
        return TransactionResult(True, balance, balance - tx.amount, None)
    return TransactionResult(False, balance, balance, REASON_INSUFFICIENT_FUNDS)


class ParseError(ValueError):
    """Riga di scenario o comando da tastiera non valido.

    ``reason`` è uno di ``PARSE_REASONS``; ``text`` è l'argomento ricevuto dalla funzione che
    solleva l'errore, senza i soli ``\\r``/``\\n`` finali (commento e spazi restano).
    ``str(e) == e.reason``.
    """

    def __init__(self, reason: str, text: str) -> None:
        """Memorizza il motivo e il testo originale (senza fine riga)."""
        super().__init__(reason)
        self.reason: str = reason
        self.text: str = text.rstrip("\r\n")


def strip_comment(line: str) -> str:
    """Rimuove tutto dal primo ``#`` in poi e gli spazi bianchi iniziali e finali. Pura."""
    return line.split("#", 1)[0].strip()


def _operation_from_words(words: list[str], text: str) -> Transaction:
    """Costruisce la transazione dalle parole già separate; ``text`` serve per ``ParseError``.

    I controlli seguono un ordine fisso, così ogni riga ha un solo motivo d'errore: parola
    sconosciuta, importo
    mancante, importo non valido, troppi argomenti.
    """
    op = _OPERATION_WORDS.get(words[0].lower()) if words else None
    if op is None:
        raise ParseError("comando_sconosciuto", text)
    if len(words) < 2:
        raise ParseError("importo_mancante", text)
    raw_amount = words[1]
    if _AMOUNT_RE.fullmatch(raw_amount) is None:
        raise ParseError("importo_non_valido", text)
    amount = int(raw_amount)
    if not 1 <= amount <= MAX_AMOUNT:
        raise ParseError("importo_non_valido", text)
    if len(words) > 2:
        raise ParseError("troppi_argomenti", text)
    return Transaction(op, amount)


def parse_operation(text: str) -> Transaction:
    """Interpreta un'operazione ``deposito|d|prelievo|p <importo>`` (maiuscole indifferenti).

    Applica ``strip_comment``; i separatori sono uno o più spazi bianchi. Solleva
    ``ParseError`` (con ``text`` come testo) se la riga non è un'operazione valida.
    """
    return _operation_from_words(strip_comment(text).split(), text)


def parse_scenario_line(line: str) -> Transaction | None:
    """Interpreta una riga di un file di scenario.

    Restituisce ``None`` per una riga vuota o di solo commento, altrimenti la
    ``Transaction``. Solleva ``ParseError`` se la riga non è valida.
    """
    if not strip_comment(line):
        return None
    return parse_operation(line)


class ScenarioError(Exception):
    """File di scenario illeggibile o con una riga non valida.

    ``path``: percorso come passato a ``load_scenario`` (come stringa); ``line_no``: riga
    1-based dell'errore, 0 per errori di I/O o di codifica; ``reason``: uno di
    ``PARSE_REASONS`` o di ``SCENARIO_IO_REASONS``.
    """

    def __init__(self, path: str, line_no: int, reason: str) -> None:
        """Memorizza i dati dell'errore; ``str(e)`` è ``"riga <n>: <reason>"`` o ``reason``."""
        message = f"riga {line_no}: {reason}" if line_no > 0 else reason
        super().__init__(message)
        self.path: str = path
        self.line_no: int = line_no
        self.reason: str = reason


def load_scenario(path: str | os.PathLike[str]) -> list[Transaction]:
    """Legge un file di scenario (UTF-8, BOM ammesso) e restituisce le transazioni in ordine.

    Si ferma al primo errore: un file con anche una sola riga sbagliata viene rifiutato per
    intero, così il nodo non parte mai con uno scenario diverso da quello scritto. Solleva
    ``ScenarioError`` con ``line_no=0`` per errori di lettura (``file_non_leggibile``) o di
    codifica (``codifica_non_valida``), altrimenti con il numero della riga sbagliata.
    """
    path_text = os.fspath(path)
    try:
        # newline=None: "\r\n" e "\n" diventano entrambi "\n" (file scritti su Windows o no).
        with open(path, "r", encoding="utf-8-sig", newline=None) as handle:
            content = handle.read()
    except UnicodeDecodeError:
        raise ScenarioError(path_text, 0, "codifica_non_valida") from None
    except OSError:
        raise ScenarioError(path_text, 0, "file_non_leggibile") from None
    transactions: list[Transaction] = []
    for line_no, line in enumerate(content.split("\n"), start=1):
        try:
            tx = parse_scenario_line(line)
        except ParseError as exc:
            raise ScenarioError(path_text, line_no, exc.reason) from None
        if tx is not None:
            transactions.append(tx)
    return transactions


@dataclass(frozen=True)
class Command:
    """Comando da tastiera: ``kind`` è una delle costanti ``CMD_*``.

    ``transaction`` è valorizzata solo per ``CMD_OPERATION``.
    """

    kind: str
    transaction: Transaction | None = None


def parse_command(line: str) -> Command | None:
    """Interpreta una riga digitata nel terminale del nodo.

    Comandi (maiuscole indifferenti): ``deposito|d N``, ``prelievo|p N``, ``stato|s``,
    ``aiuto|h|?``, ``esci|q``. Restituisce ``None`` per una riga vuota o di solo commento.
    Solleva ``ParseError`` (con la riga ricevuta come testo) se il comando non è valido;
    ``stato``, ``aiuto`` ed ``esci`` seguiti da altre parole danno ``troppi_argomenti``.
    Funzione pura.
    """
    words = strip_comment(line).split()
    if not words:
        return None
    keyword = words[0].lower()
    if keyword in _OPERATION_WORDS:
        return Command(CMD_OPERATION, _operation_from_words(words, line))
    for kind, accepted in ((CMD_STATUS, _STATUS_WORDS), (CMD_HELP, _HELP_WORDS),
                           (CMD_QUIT, _QUIT_WORDS)):
        if keyword in accepted:
            if len(words) > 1:
                raise ParseError("troppi_argomenti", line)
            return Command(kind)
    raise ParseError("comando_sconosciuto", line)
