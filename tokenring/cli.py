"""Punto d'ingresso del nodo: sequenza di avvio e arresto.

Qui si eseguono i primi cinque passi dell'avvio: (1) lettura delle opzioni, (2-3)
configurazione della console, (4) apertura del file di log, (5) caricamento dello
scenario. Il resto (porta di ascolto, thread, connessione al successore, ciclo del
token e arresto ordinato) è in ``AtmNode.run``.
"""

from __future__ import annotations

import sys
from typing import Sequence

from .config import (EXIT_BAD_SCENARIO, EXIT_INTERNAL_ERROR, EXIT_LOG_ERROR, EXIT_OK,
                     EXIT_USAGE, parse_args)
from .console import configure_stdout, disable_quick_edit, enable_ansi, set_window_title
from .logfmt import NodeLogger
from .node import AtmNode
from .transactions import ScenarioError, Transaction, load_scenario


def _system_exit_code(exc: SystemExit) -> int:
    """Converte il ``code`` di un ``SystemExit`` di argparse in un intero."""
    if exc.code is None:
        return EXIT_OK
    if isinstance(exc.code, int):
        return exc.code
    return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    """Avvia il nodo e restituisce il codice d'uscita (costanti ``EXIT_*`` di ``config``).

    ``argv`` None -> ``sys.argv[1:]``. Non chiama mai ``sys.exit``/``os._exit``: il
    ``SystemExit`` di argparse (errore -> 2, ``--help`` -> 0) viene intercettato e il suo
    codice restituito. Ctrl+C prima dell'apertura del log termina con codice 0 senza log.
    """
    if argv is None:
        argv = sys.argv[1:]
    try:
        # Passo 1 - opzioni.
        try:
            config = parse_args(argv)
        except SystemExit as exc:
            return _system_exit_code(exc)
        # Passi 2-3 - console: stdout che non solleva mai eccezioni, colori, titolo, QuickEdit.
        configure_stdout()
        use_color = config.use_color and enable_ansi()
        set_window_title(config.node_id)
        disable_quick_edit()
        # Passo 4 - file di log, aperto prima di qualunque evento.
        log_path = config.log_dir / f"{config.node_id}.log"
        try:
            logger = NodeLogger(config.node_id, log_path, use_color)
        except OSError as exc:
            sys.stderr.write(f"[{config.node_id}] ERRORE: impossibile aprire il file di log "
                             f"{log_path}: {exc}\n")
            return EXIT_LOG_ERROR
    except KeyboardInterrupt:
        return EXIT_OK

    try:
        try:
            logger.log("NODO_AVVIATO", successore=config.successor_id,
                       porta_successore=config.successor_port, ritardo=config.delay)
            # Passo 5 - scenario, caricato PRIMA di aprire la porta: quando il predecessore
            # può connettersi, la coda è già pronta e lo scenario di riferimento del
            # docente (1000 -> 800 -> 900 -> 400) è deterministico in ogni ordine di avvio.
            transactions: list[Transaction] = []
            if config.scenario_path is not None:
                try:
                    transactions = load_scenario(config.scenario_path)
                except ScenarioError as exc:
                    logger.log("ERRORE", tipo="scenario_non_valido",
                               dettaglio=f"{exc.path}: {exc}")
                    logger.log_final("ARRESTO", motivo="errore", codice=EXIT_BAD_SCENARIO)
                    return EXIT_BAD_SCENARIO
                logger.log("SCENARIO_CARICATO", transazioni=len(transactions))
            node = AtmNode(config, logger, transactions)
        except KeyboardInterrupt:
            logger.log_final("ARRESTO", motivo="interruzione", codice=EXIT_OK)
            return EXIT_OK
        except Exception as exc:
            logger.log("ERRORE", tipo="errore_interno", dettaglio=f"{type(exc).__name__}: {exc}")
            logger.log_final("ARRESTO", motivo="errore", codice=EXIT_INTERNAL_ERROR)
            return EXIT_INTERNAL_ERROR
        # Passi 6-11.
        return node.run()
    except KeyboardInterrupt:
        # Secondo Ctrl+C durante la sequenza di arresto: si esce comunque in modo ordinato.
        return EXIT_OK
    except Exception as exc:
        # Ultima rete di sicurezza (es. file di log non più scrivibile): nessun traceback.
        sys.stderr.write(f"[{config.node_id}] ERRORE: {type(exc).__name__}: {exc}\n")
        return EXIT_INTERNAL_ERROR
    finally:
        logger.close()
