"""Opzioni da riga di comando, configurazione del nodo, codici d'uscita e costanti (P1-09).

Indirizzamento (P1-09): il nodo riceve da CLI **esplicitamente** e **solo** il
proprio ID, la propria porta, l'ID e la porta del successore. Nel codice non esiste alcuna
tabella dell'anello e nessun successore o porta viene calcolato dall'ID: l'anello esiste
solo nei quattro comandi di avvio.
"""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

from .protocol import NODE_ID_PATTERN

HOST: Final[str] = "127.0.0.1"
DEFAULT_DELAY: Final[float] = 1.5
MAX_DELAY: Final[float] = 60.0
DEFAULT_INITIAL_BALANCE: Final[int] = 1000
DEFAULT_LOG_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "logs"  # token-ring-atm/logs
MIN_PORT: Final[int] = 1024
MAX_PORT: Final[int] = 65535

EXIT_OK: Final[int] = 0
EXIT_INTERNAL_ERROR: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_PORT_IN_USE: Final[int] = 3
EXIT_BAD_SCENARIO: Final[int] = 4
EXIT_RING_MISCONFIGURED: Final[int] = 5
EXIT_LOG_ERROR: Final[int] = 6


@dataclass(frozen=True)
class NodeConfig:
    """Configurazione completa di un nodo, ricavata dalle opzioni della riga di comando."""

    node_id: str                 # "ATM1".."ATM4" (normalizzato in maiuscolo)
    port: int
    successor_id: str
    successor_port: int
    create_token: bool
    initial_balance: int         # DEFAULT_INITIAL_BALANCE se --saldo-iniziale assente
    scenario_path: Path | None   # così come passato (relativo alla cartella corrente)
    delay: float
    rounds: int                  # 0 = infinito
    log_dir: Path                # Path(--cartella-log).resolve() se passata, altrimenti DEFAULT_LOG_DIR
    use_color: bool              # False se --no-colori


class _ItalianHelpFormatter(argparse.HelpFormatter):
    """Formatter di argparse con il prefisso ``uso: `` al posto di ``usage: ``."""

    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[no-untyped-def]
        """Come ``HelpFormatter.add_usage``, con prefisso italiano predefinito."""
        if prefix is None:
            prefix = "uso: "
        super().add_usage(usage, actions, groups, prefix)


def _node_id_arg(value: str) -> str:
    """Tipo argparse per un ID di nodo: ``ATM1``..``ATM4``, maiuscole indifferenti."""
    normalized = value.upper()
    if re.fullmatch(NODE_ID_PATTERN, normalized) is None:
        raise argparse.ArgumentTypeError(
            f"identificativo non valido: '{value}' (ammessi ATM1, ATM2, ATM3, ATM4)")
    return normalized


def _port_arg(value: str) -> int:
    """Tipo argparse per una porta TCP: intero tra ``MIN_PORT`` e ``MAX_PORT``."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"porta non valida: '{value}'") from None
    if not MIN_PORT <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"porta non valida: {port} (ammesse {MIN_PORT}..{MAX_PORT})")
    return port


def _non_negative_int_arg(value: str) -> int:
    """Tipo argparse per un intero maggiore o uguale a zero."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"intero non valido: '{value}'") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"valore non valido: {number} (atteso >= 0)")
    return number


def _delay_arg(value: str) -> float:
    """Tipo argparse per ``--ritardo``: numero finito tra 0 e ``MAX_DELAY`` secondi."""
    try:
        delay = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ritardo non valido: '{value}'") from None
    if not math.isfinite(delay) or not 0.0 <= delay <= MAX_DELAY:
        raise argparse.ArgumentTypeError(
            f"ritardo non valido: '{value}' (ammesso da 0 a {MAX_DELAY:g} secondi)")
    return delay


def build_arg_parser() -> argparse.ArgumentParser:
    """Costruisce il parser delle opzioni del nodo, con testi di aiuto in italiano."""
    parser = argparse.ArgumentParser(
        prog="atm_node.py",
        description="Nodo ATM dell'anello Token Ring (mutua esclusione sul saldo condiviso).",
        add_help=False,
        formatter_class=_ItalianHelpFormatter,
    )
    parser.add_argument("-h", "--help", action="help",
                        help="mostra questo messaggio ed esce")
    parser.add_argument("--id", dest="node_id", type=_node_id_arg, required=True,
                        metavar="ID", help="identificativo di questo nodo (ATM1..ATM4)")
    parser.add_argument("--porta", dest="port", type=_port_arg, required=True,
                        metavar="PORTA",
                        help="porta TCP su cui questo nodo ascolta il predecessore")
    parser.add_argument("--id-successore", dest="successor_id", type=_node_id_arg,
                        required=True, metavar="ID",
                        help="identificativo del nodo successore nell'anello")
    parser.add_argument("--porta-successore", dest="successor_port", type=_port_arg,
                        required=True, metavar="PORTA", help="porta TCP del nodo successore")
    parser.add_argument("--crea-token", dest="create_token", action="store_true",
                        help="questo nodo crea il token iniziale (uno solo nell'anello)")
    parser.add_argument("--saldo-iniziale", dest="initial_balance",
                        type=_non_negative_int_arg, default=None, metavar="SALDO",
                        help="saldo iniziale nel token creato (predefinito 1000)")
    parser.add_argument("--scenario", dest="scenario_path", type=Path, default=None,
                        metavar="FILE",
                        help="file con le transazioni di questo nodo, una per riga")
    parser.add_argument("--ritardo", dest="delay", type=_delay_arg, default=DEFAULT_DELAY,
                        metavar="SECONDI",
                        help="secondi di pausa prima di ogni inoltro del token (0 = immediato)")
    parser.add_argument("--giri", dest="rounds", type=_non_negative_int_arg, default=0,
                        metavar="N",
                        help="termina dopo N giri del token (0 = mai; usato nei test)")
    parser.add_argument("--cartella-log", dest="log_dir", default=None, metavar="CARTELLA",
                        help="cartella dei file di log (predefinita: logs accanto ad atm_node.py)")
    parser.add_argument("--no-colori", dest="no_color", action="store_true",
                        help="disattiva i colori ANSI sul terminale")
    return parser


def parse_args(argv: Sequence[str]) -> NodeConfig:
    """Interpreta e valida le opzioni; restituisce la ``NodeConfig``.

    Su errore argparse scrive uso e messaggio su stderr e solleva ``SystemExit(2)``; con
    ``-h``/``--help`` scrive l'aiuto su stdout e solleva ``SystemExit(0)``.
    """
    parser = build_arg_parser()
    args = parser.parse_args(list(argv))
    # Validazioni incrociate: un nodo non può essere successore di se stesso, e un saldo
    # iniziale ha senso solo per il nodo che crea il token: meglio un errore esplicito che
    # un'opzione accettata e poi ignorata in silenzio.
    if args.node_id == args.successor_id:
        parser.error("--id e --id-successore devono essere diversi")
    if args.port == args.successor_port:
        parser.error("--porta e --porta-successore devono essere diverse")
    if args.initial_balance is not None and not args.create_token:
        parser.error("--saldo-iniziale e' ammessa solo insieme a --crea-token")
    return NodeConfig(
        node_id=args.node_id,
        port=args.port,
        successor_id=args.successor_id,
        successor_port=args.successor_port,
        create_token=args.create_token,
        initial_balance=(args.initial_balance if args.initial_balance is not None
                         else DEFAULT_INITIAL_BALANCE),
        scenario_path=args.scenario_path,
        delay=args.delay,
        rounds=args.rounds,
        log_dir=Path(args.log_dir).resolve() if args.log_dir is not None else DEFAULT_LOG_DIR,
        use_color=not args.no_color,
    )
