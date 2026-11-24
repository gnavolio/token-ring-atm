"""Test d'integrazione della riga di comando e dei codici d'uscita."""
from __future__ import annotations

import socket
import unittest

import support as S

TIMEOUT = 15.0


class CliExitCodesTest(unittest.TestCase):
    def setUp(self):
        S.require_code()
        self.h = S.Harness()
        self.addCleanup(self.h.close)
        _, self.ports = S.allocate_block()

    def base(self, log_dir=None):
        p = self.ports
        return ["atm_node.py", "--id", "ATM1", "--porta", str(p["ATM1"]), "--id-successore", "ATM2",
                "--porta-successore", str(p["ATM2"]), "--ritardo", "0.1", "--no-colori",
                "--cartella-log", str(log_dir or self.h.log_dir)]

    def run_node(self, args):
        return S.run_python(args, timeout=TIMEOUT)

    def describe(self, proc, log_path=None) -> str:
        out = [f"returncode={proc.returncode}",
               "stdout: " + proc.stdout.decode("utf-8", "replace")[-2000:],
               "stderr: " + proc.stderr.decode("utf-8", "replace")[-2000:]]
        if log_path is not None:
            out.append("log:\n" + S.read_text(log_path))
        return "\n".join(out)

    def test_invalid_arguments_exit_2_without_log(self):
        """Argomenti non validi -> codice 2, messaggio 'uso:' su stderr,
        nessun file di log creato."""
        p = self.ports
        base = ["atm_node.py", "--id", "ATM1", "--porta", str(p["ATM1"]), "--id-successore", "ATM2",
                "--porta-successore", str(p["ATM2"])]
        log_dir = self.h.tmp / "non_deve_esistere"
        cases = {
            "manca --porta-successore": base[:-2],
            "--porta 80": ["atm_node.py", "--id", "ATM1", "--porta", "80", "--id-successore",
                           "ATM2", "--porta-successore", str(p["ATM2"])],
            "--id ATM5": ["atm_node.py", "--id", "ATM5", "--porta", str(p["ATM1"]),
                          "--id-successore", "ATM2", "--porta-successore", str(p["ATM2"])],
            "--ritardo 61": base + ["--ritardo", "61"],
            "--id = --id-successore": ["atm_node.py", "--id", "ATM1", "--porta", str(p["ATM1"]),
                                       "--id-successore", "ATM1", "--porta-successore",
                                       str(p["ATM2"])],
            "--saldo-iniziale senza --crea-token": base + ["--saldo-iniziale", "10"],
        }
        for name, args in cases.items():
            with self.subTest(name):
                proc = self.run_node(args + ["--cartella-log", str(log_dir)])
                self.assertEqual(proc.returncode, 2, self.describe(proc))
                self.assertIn("uso:", proc.stderr.decode("utf-8", "replace"), self.describe(proc))
                self.assertFalse(log_dir.exists(), "creata la cartella dei log")

    def test_help_exit_0(self):
        """--help -> codice 0, aiuto su stdout, nessun log."""
        log_dir = self.h.tmp / "help_logs"
        proc = self.run_node(["atm_node.py", "--help", "--cartella-log", str(log_dir)])
        self.assertEqual(proc.returncode, 0, self.describe(proc))
        text = proc.stdout.decode("utf-8", "replace")
        self.assertTrue(text.startswith("uso: "), self.describe(proc))
        self.assertIn("--porta-successore", text)
        self.assertFalse(log_dir.exists())

    def test_port_in_use_exit_3(self):
        """Porta di ascolto occupata -> ERRORE tipo=porta_occupata,
        ARRESTO motivo=errore codice=3 come ultima riga, codice d'uscita 3, nessun IN_ASCOLTO."""
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", self.ports["ATM1"]))
        blocker.listen(1)
        proc = self.run_node(self.base())
        log = self.h.log_dir / "ATM1.log"
        self.assertEqual(proc.returncode, 3, self.describe(proc, log))
        lines = S.read_complete_lines(log)
        entries, _ = S.parse_log("ATM1", lines)
        self.assertIn("porta_occupata", [e.fields.get("tipo") for e in entries if e.event == "ERRORE"],
                      self.describe(proc, log))
        self.assertTrue(lines and lines[-1].endswith("ARRESTO               motivo=errore codice=3"),
                        self.describe(proc, log))
        self.assertNotIn("IN_ASCOLTO", [e.event for e in entries])
        errs = S.check_format("ATM1", lines, True, S.read_text(log))
        self.assertEqual(errs, [], S.format_errors(errs))

    def test_invalid_scenario_exit_4(self):
        """P1-I7: scenario con 'PRELIEVO -5' o inesistente -> ERRORE
        tipo=scenario_non_valido, ARRESTO motivo=errore codice=4, codice 4; il nodo non apre la
        porta (nessun IN_ASCOLTO, porta libera)."""
        bad = self.h.tmp / "bad.txt"
        bad.write_text("# scenario errato\nPRELIEVO -5\n", encoding="utf-8")
        for label, path in (("PRELIEVO -5", bad), ("file inesistente", self.h.tmp / "nope.txt")):
            with self.subTest(label):
                log_dir = self.h.tmp / f"logs_{label.replace(' ', '_')}"
                proc = self.run_node(self.base(log_dir) + ["--scenario", str(path)])
                log = log_dir / "ATM1.log"
                self.assertEqual(proc.returncode, 4, self.describe(proc, log))
                lines = S.read_complete_lines(log)
                entries, _ = S.parse_log("ATM1", lines)
                self.assertIn("scenario_non_valido",
                              [e.fields.get("tipo") for e in entries if e.event == "ERRORE"],
                              self.describe(proc, log))
                self.assertTrue(lines and lines[-1].endswith(
                    "ARRESTO               motivo=errore codice=4"), self.describe(proc, log))
                events = [e.event for e in entries]
                self.assertNotIn("IN_ASCOLTO", events)
                self.assertNotIn("SCENARIO_CARICATO", events)
                self.assertTrue(S.port_is_free(self.ports["ATM1"]))
                errs = S.check_format("ATM1", lines, True, S.read_text(log))
                self.assertEqual(errs, [], S.format_errors(errs))

    def test_log_dir_unusable_exit_6(self):
        """P1-I8: cartella dei log non creabile -> codice 6, messaggio
        '[ATM1] ERRORE: impossibile aprire il file di log' su stderr."""
        blocker = self.h.tmp / "un_file"
        blocker.write_text("x", encoding="utf-8")
        proc = self.run_node(self.base(blocker / "sub"))
        self.assertEqual(proc.returncode, 6, self.describe(proc))
        self.assertTrue(proc.stderr.decode("utf-8", "replace").startswith(
            "[ATM1] ERRORE: impossibile aprire il file di log"), self.describe(proc))


if __name__ == "__main__":
    unittest.main()
