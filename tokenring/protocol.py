"""Protocollo di rete dell'anello: il messaggio ``TOKEN`` (scambio di messaggi, P1-04, P1-05).

Framing
    Ogni nodo mantiene una connessione TCP persistente verso il proprio successore su
    127.0.0.1. Ogni messaggio è un oggetto JSON codificato UTF-8 su **una sola riga**,
    terminato da ``\\n``. TCP è un flusso di byte senza confini di messaggio: il ricevente
    accumula i byte e separa i messaggi sulle ``\\n`` (vedi ``AtmNode._serve_predecessor``).

Un solo tipo di messaggio
    Esiste soltanto ``TOKEN``. Non ci sono richieste, risposte, ack, heartbeat o messaggi di
    controllo: con un solo tipo di messaggio, che rappresenta il permesso di entrare in
    sezione critica, è impossibile coordinarsi in un modo diverso dal Token Ring (P1-05).
    Anche l'arresto dopo N giri e la rilevazione del successore usano solo eventi locali e
    lo stato della connessione TCP.

Il saldo viaggia solo qui
    La chiave ``balance`` del token è l'**unica copia autorevole** del saldo del conto
    tra i processi: nessun file, database o memoria condivisa (P1-10, P1-I1). Chi possiede
    il token possiede anche il saldo, quindi la mutua esclusione sul token è anche mutua
    esclusione sul saldo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

MSG_TYPE_TOKEN: Final[str] = "TOKEN"
MAX_LINE_BYTES: Final[int] = 4096          # lunghezza massima di una riga ricevuta, newline escluso
NODE_ID_PATTERN: Final[str] = r"ATM[1-4]"  # da usare con re.fullmatch

# Chiavi del messaggio TOKEN, nell'ordine in cui vengono serializzate.
_TOKEN_KEYS: Final[tuple[str, ...]] = ("type", "seq", "balance", "sender", "receiver")


class ProtocolError(ValueError):
    """Messaggio ricevuto non conforme al protocollo. str(e) è un testo italiano."""


def _is_non_negative_int(value: object) -> bool:
    """True se ``value`` è un ``int`` (non ``bool``) maggiore o uguale a zero."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_valid_node_id(value: object) -> bool:
    """Restituisce True se ``value`` è una stringa ``ATM1``..``ATM4`` (confronto esatto)."""
    return isinstance(value, str) and re.fullmatch(NODE_ID_PATTERN, value) is not None


@dataclass(frozen=True)
class Token:
    """Il token dell'anello: permesso di sezione critica e unica copia del saldo.

    Campi:
    - ``seq``: numero di sequenza. Il token creato ha ``seq=0`` e ogni inoltro (hop) produce
      un token con ``seq + 1``. Permette di verificare dai log che il token è unico (ogni
      ``seq`` viene ricevuto al più una volta) e che i possessi sono ordinati;
    - ``balance``: il saldo del conto, unica copia autorevole (P1-I1);
    - ``sender``: nodo che ha inoltrato il token; usato **solo** per il log (``da=``), mai
      per inviare qualcosa (P1-09);
    - ``receiver``: nodo a cui il mittente lo ha destinato (il suo ``--id-successore``). Il
      ricevente lo confronta con il proprio ID: se differisce, l'anello è configurato male
      (porta del successore sbagliata in un comando di avvio) e il nodo si ferma con
      codice 5, segnalando l'errore invece di eseguire transazioni su un anello sbagliato.

    Il token è **immutabile** (``frozen=True``): un token ricevuto non viene mai modificato
    sul posto. La sezione critica e l'inoltro costruiscono un nuovo oggetto
    (``dataclasses.replace``, ``make_forward_token``), così nessun riferimento rimasto in una
    coda o in un log può vedere un saldo intermedio.
    """

    seq: int
    balance: int
    sender: str
    receiver: str

    def __post_init__(self) -> None:
        """Valida i campi; solleva ``ValueError`` se un vincolo è violato."""
        if not _is_non_negative_int(self.seq):
            raise ValueError(f"seq non valido: {self.seq!r} (atteso intero >= 0)")
        if not _is_non_negative_int(self.balance):
            raise ValueError(f"balance non valido: {self.balance!r} (atteso intero >= 0)")
        if not is_valid_node_id(self.sender):
            raise ValueError(f"sender non valido: {self.sender!r} (atteso ATM1..ATM4)")
        if not is_valid_node_id(self.receiver):
            raise ValueError(f"receiver non valido: {self.receiver!r} (atteso ATM1..ATM4)")


def create_initial_token(node_id: str, balance: int) -> Token:
    """Crea il token iniziale: ``seq=0``, mittente e destinatario uguali al creatore.

    Il token creato non viaggia mai in rete: il creatore lo prende subito in carico e il
    primo token trasmesso ha ``seq=1``. Solleva ``ValueError`` se ``node_id`` non è valido o
    ``balance`` non è un intero >= 0. Funzione pura.
    """
    return Token(seq=0, balance=balance, sender=node_id, receiver=node_id)


def make_forward_token(token: Token, sender: str, receiver: str) -> Token:
    """Costruisce il token da inoltrare al successore (un hop).

    ``seq`` aumenta di 1, il saldo resta quello del token in mano (già aggiornato dalla
    eventuale transazione), ``sender``/``receiver`` sono questo nodo e il suo successore.
    Il token originale non viene modificato. Solleva ``ValueError`` se ``sender`` o
    ``receiver`` non sono validi. Funzione pura.
    """
    return Token(seq=token.seq + 1, balance=token.balance, sender=sender, receiver=receiver)


def encode_token(token: Token) -> bytes:
    """Serializza il token in una riga JSON UTF-8 terminata da ``\\n``.

    Formato esatto: JSON compatto (nessuno spazio), chiavi nell'ordine
    ``type, seq, balance, sender, receiver``, solo ASCII. Esempio::

        b'{"type":"TOKEN","seq":1,"balance":1000,"sender":"ATM1","receiver":"ATM2"}\\n'

    Il ``\\n`` finale è il delimitatore del messaggio nel flusso TCP; il JSON compatto non
    contiene mai ``\\n`` al suo interno, quindi il framing per righe è sempre corretto.
    Funzione pura.
    """
    payload = {
        "type": MSG_TYPE_TOKEN,
        "seq": token.seq,
        "balance": token.balance,
        "sender": token.sender,
        "receiver": token.receiver,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_message(line: bytes) -> Token:
    """Decodifica una riga ricevuta e restituisce il ``Token``.

    Accetta la riga con o senza ``\\n`` / ``\\r\\n`` finali (rimossi con ``.strip()``).
    Solleva ``ProtocolError`` (messaggio in italiano) se: i byte non sono UTF-8; il testo non
    è JSON; il JSON non è un oggetto; le chiavi non sono esattamente
    ``{type, seq, balance, sender, receiver}``; ``type`` non è ``"TOKEN"``; ``seq`` o
    ``balance`` non sono interi >= 0 (booleani e decimali come ``1000.0`` rifiutati);
    ``sender`` o ``receiver`` non sono ``ATM1``..``ATM4``.

    Perché la validazione è così severa: il token trasporta l'unica copia del saldo, quindi
    un messaggio ambiguo non deve mai diventare un token. Il chiamante (thread di ascolto)
    registra l'errore e **scarta solo quella riga** senza fermare il nodo: un messaggio
    spurio non è un token e non deve far perdere quello vero. Funzione pura.
    """
    try:
        text = bytes(line).strip().decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("byte non UTF-8") from None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        # json.JSONDecodeError è una sottoclasse di ValueError; RecursionError copre un JSON
        # annidato in modo patologico.
        raise ProtocolError("JSON non valido") from None
    if not isinstance(data, dict):
        raise ProtocolError("il messaggio non e' un oggetto JSON")
    if set(data) != set(_TOKEN_KEYS):
        raise ProtocolError("chiavi non valide: attese type, seq, balance, sender, receiver")
    if data["type"] != MSG_TYPE_TOKEN:
        raise ProtocolError("tipo di messaggio sconosciuto")
    if not _is_non_negative_int(data["seq"]):
        raise ProtocolError("seq non valido: atteso intero >= 0")
    if not _is_non_negative_int(data["balance"]):
        raise ProtocolError("balance non valido: atteso intero >= 0")
    if not is_valid_node_id(data["sender"]):
        raise ProtocolError("sender non valido: atteso ATM1..ATM4")
    if not is_valid_node_id(data["receiver"]):
        raise ProtocolError("receiver non valido: atteso ATM1..ATM4")
    return Token(seq=data["seq"], balance=data["balance"],
                 sender=data["sender"], receiver=data["receiver"])
