"""Test unitari di ``tokenring/config.py`` e ``tokenring/cli.main``."""
from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

import support as S
from tokenring import cli, config
from tokenring.config import NodeConfig, build_arg_parser, parse_args

# Comandi esatti di avvio della demo (senza "python atm_node.py").
DEMO_COMMANDS = {
    "ATM1": "--id ATM1 --porta 5001 --id-successore ATM2 --porta-successore 5002 --crea-token "
            "--scenario scenari/riferimento/ATM1.txt --ritardo 1.5",
    "ATM2": "--id ATM2 --porta 5002 --id-successore ATM3 --porta-successore 5003 "
            "--scenario scenari/riferimento/ATM2.txt --ritardo 1.5",
    "ATM3": "--id ATM3 --porta 5003 --id-successore ATM4 --porta-successore 5004 "
            "--scenario scenari/riferimento/ATM3.txt --ritardo 1.5",
    "ATM4": "--id ATM4 --porta 5004 --id-successore ATM1 --porta-successore 5001 "
            "--scenario scenari/riferimento/ATM4.txt --ritardo 1.5",
}
BASE = ["--id", "ATM2", "--porta", "5002", "--id-successore", "ATM3", "--porta-successore", "5003"]

HELP_TEXTS = {
    "--help": "mostra questo messaggio ed esce",
    "--id": "identificativo di questo nodo (ATM1..ATM4)",
    "--porta": "porta TCP su cui questo nodo ascolta il predecessore",
    "--id-successore": "identificativo del nodo successore nell'anello",
    "--porta-successore": "porta TCP del nodo successore",
    "--crea-token": "questo nodo crea il token iniziale (uno solo nell'anello)",
    "--saldo-iniziale": "saldo iniziale nel token creato (predefinito 1000)",
    "--scenario": "file con le transazioni di questo nodo, una per riga",
    "--ritardo": "secondi di pausa prima di ogni inoltro del token (0 = immediato)",
    "--giri": "termina dopo N giri del token (0 = mai; usato nei test)",
    "--cartella-log": "cartella dei file di log (predefinita: logs accanto ad atm_node.py)",
    "--no-colori": "disattiva i colori ANSI sul terminale",
}


def _parse_quiet(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = parse_args(argv)
    return result, out.getvalue(), err.getvalue()


class ConfigConstantsTest(unittest.TestCase):
    def test_constants(self):
        """P1-02, P1-18: costanti di configurazione (indirizzo 127.0.0.1, saldo iniziale 1000, limiti)."""
        self.assertEqual(config.HOST, "127.0.0.1")
        self.assertEqual(config.DEFAULT_DELAY, 1.5)
        self.assertEqual(config.MAX_DELAY, 60.0)
        self.assertEqual(config.DEFAULT_INITIAL_BALANCE, 1000)
        self.assertEqual((config.MIN_PORT, config.MAX_PORT), (1024, 65535))
        self.assertEqual(Path(config.DEFAULT_LOG_DIR).resolve(), (S.ROOT / "logs").resolve())

    def test_exit_codes(self):
        """Codici d'uscita: 0 ok, 1 errore interno, 2 uso, 3 porta occupata, 4 scenario, 5 anello
        mal configurato, 6 file di log."""
        self.assertEqual((config.EXIT_OK, config.EXIT_INTERNAL_ERROR, config.EXIT_USAGE,
                          config.EXIT_PORT_IN_USE, config.EXIT_BAD_SCENARIO,
                          config.EXIT_RING_MISCONFIGURED, config.EXIT_LOG_ERROR),
                         (0, 1, 2, 3, 4, 5, 6))


class ParseArgsValidTest(unittest.TestCase):
    def test_demo_commands(self):
        """P1-09, P1-18: i 4 comandi di avvio della demo producono la NodeConfig attesa."""
        ports = {"ATM1": 5001, "ATM2": 5002, "ATM3": 5003, "ATM4": 5004}
        for node, cmd in DEMO_COMMANDS.items():
            with self.subTest(node=node):
                cfg, _, _ = _parse_quiet(cmd.split())
                succ = S.SUCCESSOR[node]
                expected = NodeConfig(
                    node_id=node, port=ports[node], successor_id=succ,
                    successor_port=ports[succ], create_token=(node == "ATM1"),
                    initial_balance=1000,
                    scenario_path=Path(f"scenari/riferimento/{node}.txt"), delay=1.5, rounds=0,
                    log_dir=config.DEFAULT_LOG_DIR, use_color=True)
                self.assertEqual(cfg, expected)

    def test_id_normalized_to_upper(self):
        """P1-09: --id e --id-successore senza distinzione di maiuscole, normalizzati."""
        cfg, _, _ = _parse_quiet(["--id", "atm2", "--porta", "5002", "--id-successore", "Atm3",
                                  "--porta-successore", "5003"])
        self.assertEqual((cfg.node_id, cfg.successor_id), ("ATM2", "ATM3"))

    def test_defaults(self):
        """Valori predefiniti neutri delle opzioni facoltative."""
        cfg, _, _ = _parse_quiet(BASE)
        self.assertEqual((cfg.create_token, cfg.initial_balance, cfg.scenario_path, cfg.delay,
                          cfg.rounds, cfg.use_color), (False, 1000, None, 1.5, 0, True))
        self.assertEqual(Path(cfg.log_dir), Path(config.DEFAULT_LOG_DIR))

    def test_test_support_options(self):
        """P1-I4: --ritardo, --giri, --cartella-log (risolta), --no-colori, --saldo-iniziale."""
        cfg, _, _ = _parse_quiet(BASE + ["--ritardo", "0", "--giri", "3", "--cartella-log", "xyz",
                                         "--no-colori"])
        self.assertEqual((cfg.delay, cfg.rounds, cfg.use_color), (0.0, 3, False))
        self.assertEqual(Path(cfg.log_dir), Path("xyz").resolve())
        cfg, _, _ = _parse_quiet(BASE + ["--crea-token", "--saldo-iniziale", "0", "--ritardo", "60"])
        self.assertEqual((cfg.create_token, cfg.initial_balance, cfg.delay), (True, 0, 60.0))
        cfg, _, _ = _parse_quiet(["--id", "ATM1", "--porta", "1024", "--id-successore", "ATM2",
                                  "--porta-successore", "65535", "--ritardo", "0.1"])
        self.assertEqual((cfg.port, cfg.successor_port, cfg.delay), (1024, 65535, 0.1))


class ParseArgsInvalidTest(unittest.TestCase):
    def test_invalid_arguments_exit_2(self):
        """Argomenti non validi -> SystemExit(2) con messaggio 'uso:' su stderr."""
        def without(opt):
            i = BASE.index(opt)
            return BASE[:i] + BASE[i + 2:]

        def replace(opt, value):
            out = list(BASE)
            out[out.index(opt) + 1] = value
            return out

        cases = {
            "manca --id": without("--id"),
            "manca --porta": without("--porta"),
            "manca --id-successore": without("--id-successore"),
            "manca --porta-successore": without("--porta-successore"),
            "--porta 80": replace("--porta", "80"),
            "--porta 1023": replace("--porta", "1023"),
            "--porta 70000": replace("--porta", "70000"),
            "--porta 65536": replace("--porta", "65536"),
            "--porta abc": replace("--porta", "abc"),
            "--porta-successore 0": replace("--porta-successore", "0"),
            "--id ATM5": replace("--id", "ATM5"),
            "--id ATM0": replace("--id", "ATM0"),
            "--id X": replace("--id", "X"),
            "--id-successore ATM9": replace("--id-successore", "ATM9"),
            "--id = --id-successore": replace("--id-successore", "atm2"),
            "--porta = --porta-successore": replace("--porta-successore", "5002"),
            "--ritardo -1": BASE + ["--ritardo", "-1"],
            "--ritardo 61": BASE + ["--ritardo", "61"],
            "--ritardo nan": BASE + ["--ritardo", "nan"],
            "--ritardo inf": BASE + ["--ritardo", "inf"],
            "--ritardo abc": BASE + ["--ritardo", "abc"],
            "--giri -1": BASE + ["--giri", "-1"],
            "--giri 1.5": BASE + ["--giri", "1.5"],
            "--saldo-iniziale senza --crea-token": BASE + ["--saldo-iniziale", "500"],
            "--saldo-iniziale -1": BASE + ["--crea-token", "--saldo-iniziale", "-1"],
            "opzione sconosciuta": BASE + ["--pippo"],
        }
        for name, argv in cases.items():
            with self.subTest(name):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        parse_args(argv)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("uso:", err.getvalue())

    def test_help_exit_0(self):
        """-h/--help -> aiuto in italiano su stdout, SystemExit(0)."""
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        parse_args([flag])
                self.assertEqual(cm.exception.code, 0)
                self.assertTrue(out.getvalue().startswith("uso: "), out.getvalue()[:80])
                self.assertIn("atm_node.py", out.getvalue())


class ArgParserTextsTest(unittest.TestCase):
    def test_parser_texts(self):
        """Nome del programma, descrizione e testi d'aiuto esatti, in italiano."""
        parser = build_arg_parser()
        self.assertEqual(parser.prog, "atm_node.py")
        self.assertEqual(parser.description,
                         "Nodo ATM dell'anello Token Ring (mutua esclusione sul saldo condiviso).")
        helps = {}
        for action in parser._actions:   # API privata ma stabile di argparse
            for opt in action.option_strings:
                helps[opt] = action.help
        for opt, text in HELP_TEXTS.items():
            with self.subTest(opt=opt):
                self.assertEqual(helps.get(opt), text)
        self.assertEqual(helps.get("-h"), HELP_TEXTS["--help"])


class CliMainTest(unittest.TestCase):
    def test_main_returns_code_without_exiting(self):
        """main() restituisce 2 su argomenti non validi e 0 con --help, senza
        sollevare SystemExit."""
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--id", "ATM5"]), 2)
            self.assertEqual(cli.main(BASE + ["--porta", "80"]), 2)
            self.assertEqual(cli.main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
