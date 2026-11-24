"""Test unitari del parser dello scenario."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import support as S
from tokenring.transactions import (
    DEPOSIT, MAX_AMOUNT, PARSE_REASONS, SCENARIO_IO_REASONS, WITHDRAWAL, ParseError,
    ScenarioError, Transaction, load_scenario, parse_operation, parse_scenario_line, strip_comment,
)

# Contenuto esatto dei file di scenario.
REFERENCE_FILES = {
    "ATM1": "# Scenario di riferimento (PDF, paragrafo 7) - nodo ATM1\n"
            "# ATM1 possiede inizialmente il token e non esegue transazioni.\n",
    "ATM2": "# Scenario di riferimento (PDF, paragrafo 7) - nodo ATM2\nPRELIEVO 200\n",
    "ATM3": "# Scenario di riferimento (PDF, paragrafo 7) - nodo ATM3\nDEPOSITO 100\n",
    "ATM4": "# Scenario di riferimento (PDF, paragrafo 7) - nodo ATM4\nPRELIEVO 500\n",
}
REFUSAL_FILES = {
    "ATM1": "# Scenario con prelievo rifiutato - nodo ATM1\n"
            "# ATM1 possiede inizialmente il token e non esegue transazioni.\n",
    "ATM2": "# Scenario con prelievo rifiutato - nodo ATM2\n"
            "# Prelievo superiore al saldo (1000): deve essere RIFIUTATO.\nPRELIEVO 1500\n",
    "ATM3": "# Scenario con prelievo rifiutato - nodo ATM3\nDEPOSITO 100\n",
    "ATM4": "# Scenario con prelievo rifiutato - nodo ATM4\n"
            "# Prelievo pari all'intero saldo (1100): deve essere ACCETTATO (saldo 0).\n"
            "PRELIEVO 1100\n",
}
REFERENCE_TX = {"ATM1": [], "ATM2": [Transaction(WITHDRAWAL, 200)],
                "ATM3": [Transaction(DEPOSIT, 100)], "ATM4": [Transaction(WITHDRAWAL, 500)]}
REFUSAL_TX = {"ATM1": [], "ATM2": [Transaction(WITHDRAWAL, 1500)],
              "ATM3": [Transaction(DEPOSIT, 100)], "ATM4": [Transaction(WITHDRAWAL, 1100)]}


class ReasonsConstantsTest(unittest.TestCase):
    def test_reasons(self):
        """P1-I7: motivi d'errore del parser e di I/O."""
        self.assertEqual(PARSE_REASONS, ("comando_sconosciuto", "importo_mancante",
                                         "importo_non_valido", "troppi_argomenti"))
        self.assertEqual(SCENARIO_IO_REASONS, ("file_non_leggibile", "codifica_non_valida"))


class StripCommentTest(unittest.TestCase):
    def test_strip_comment(self):
        """P1-I7: rimuove dal primo '#' in poi e gli spazi iniziali/finali."""
        cases = {
            "deposito 10 # nota": "deposito 10",
            "# solo commento": "",
            "   ": "",
            "": "",
            "\tp 5\t": "p 5",
            "  PRELIEVO 200  ": "PRELIEVO 200",
            "a # b # c": "a",
            "PRELIEVO 200\n": "PRELIEVO 200",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(strip_comment(line), expected)


class ParseOperationTest(unittest.TestCase):
    def test_valid_operations(self):
        """P1-07, P1-I7: parole e alias, maiuscole/minuscole, spazi multipli, tab, commento."""
        cases = {
            "deposito 100": Transaction(DEPOSIT, 100),
            "DEPOSITO 100": Transaction(DEPOSIT, 100),
            "Deposito 100": Transaction(DEPOSIT, 100),
            "d 100": Transaction(DEPOSIT, 100),
            "D 100": Transaction(DEPOSIT, 100),
            "prelievo 5": Transaction(WITHDRAWAL, 5),
            "PRELIEVO 200": Transaction(WITHDRAWAL, 200),
            "p 5": Transaction(WITHDRAWAL, 5),
            "P 5": Transaction(WITHDRAWAL, 5),
            "  prelievo    200  ": Transaction(WITHDRAWAL, 200),
            "prelievo\t200": Transaction(WITHDRAWAL, 200),
            "deposito 100 # commento in coda": Transaction(DEPOSIT, 100),
            "deposito 007": Transaction(DEPOSIT, 7),
            "deposito 0000000001": Transaction(DEPOSIT, 1),
            "deposito 1000000000": Transaction(DEPOSIT, MAX_AMOUNT),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_operation(text), expected)

    def assertReason(self, func, text, reason):
        with self.assertRaises(ParseError) as cm:
            func(text)
        self.assertEqual(cm.exception.reason, reason, f"testo {text!r}")
        self.assertEqual(str(cm.exception), reason)
        return cm.exception

    def test_error_reasons(self):
        """P1-I7: classificazione degli errori, controllati in quest'ordine: comando,
        importo mancante, importo non valido, troppi argomenti."""
        cases = {
            "versa 100": "comando_sconosciuto",
            "versa": "comando_sconosciuto",
            "versa 10 20": "comando_sconosciuto",
            "stato": "comando_sconosciuto",       # parse_operation: solo deposito/prelievo
            "deposito": "importo_mancante",
            "p": "importo_mancante",
            "P  # solo commento": "importo_mancante",
            "deposito -5": "importo_non_valido",
            "deposito +5": "importo_non_valido",
            "deposito 10.5": "importo_non_valido",
            "deposito 1e3": "importo_non_valido",
            "deposito 0": "importo_non_valido",
            "deposito 00": "importo_non_valido",
            "deposito abc": "importo_non_valido",
            "deposito 12345678901": "importo_non_valido",     # 11 cifre
            "deposito 00000000001": "importo_non_valido",     # 11 cifre anche se vale 1
            "deposito 1000000001": "importo_non_valido",      # > MAX_AMOUNT
            "deposito ١٠": "importo_non_valido",    # cifre non [0-9]
            "deposito 1_000": "importo_non_valido",
            "deposito abc def": "importo_non_valido",         # il controllo 3 precede il 4
            "deposito 10 20": "troppi_argomenti",
            "prelievo 10 abc": "troppi_argomenti",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                self.assertReason(parse_operation, text, reason)


class ParseScenarioLineTest(unittest.TestCase):
    def test_empty_and_comment_lines(self):
        """P1-I7: riga vuota o solo commento -> None."""
        for line in ("", "   ", "\n", "\r\n", "# commento", "   # commento", "\t#x\n"):
            with self.subTest(line=line):
                self.assertIsNone(parse_scenario_line(line))

    def test_operation_lines(self):
        """P1-I7: riga con operazione (anche con fine riga) -> Transaction."""
        self.assertEqual(parse_scenario_line("PRELIEVO 200\n"), Transaction(WITHDRAWAL, 200))
        self.assertEqual(parse_scenario_line("DEPOSITO 100\r\n"), Transaction(DEPOSIT, 100))

    def test_errors_propagate(self):
        """P1-I7: righe errate sollevano ParseError."""
        with self.assertRaises(ParseError) as cm:
            parse_scenario_line("PRELIEVO -5\n")
        self.assertEqual(cm.exception.reason, "importo_non_valido")


class ParseErrorTest(unittest.TestCase):
    def test_str_and_text(self):
        """P1-I7: str(e) == e.reason; e.text = argomento senza \\r\\n finali, commento e spazi
        conservati."""
        with self.assertRaises(ParseError) as cm:
            parse_operation("  versa 10  # x\r\n")
        e = cm.exception
        self.assertIsInstance(e, ValueError)
        self.assertEqual(str(e), "comando_sconosciuto")
        self.assertEqual(e.text, "  versa 10  # x")

    def test_constructor(self):
        """P1-I7: costruttore ParseError(reason, text)."""
        e = ParseError("troppi_argomenti", "stato extra")
        self.assertEqual((e.reason, e.text, str(e)), ("troppi_argomenti", "stato extra",
                                                      "troppi_argomenti"))

    def test_scenario_error_str(self):
        """P1-I7: str(ScenarioError) e' 'riga N: motivo', oppure solo il motivo per gli errori di I/O."""
        e = ScenarioError("x.txt", 2, "importo_non_valido")
        self.assertEqual((e.path, e.line_no, e.reason), ("x.txt", 2, "importo_non_valido"))
        self.assertEqual(str(e), "riga 2: importo_non_valido")
        self.assertEqual(str(ScenarioError("x.txt", 0, "file_non_leggibile")), "file_non_leggibile")


class LoadScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tokenring_unit_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name: str, data: bytes) -> str:
        p = self.tmp / name
        p.write_bytes(data)
        return str(p)

    def test_reference_and_refusal_contents(self):
        """P1-19, P1-I2, P1-I7: i contenuti degli scenari riferimento e rifiuto producono le transazioni attese."""
        for label, files, txs in (("riferimento", REFERENCE_FILES, REFERENCE_TX),
                                  ("rifiuto", REFUSAL_FILES, REFUSAL_TX)):
            for node, content in files.items():
                with self.subTest(scenario=label, node=node):
                    path = self.write(f"{label}_{node}.txt", content.encode("utf-8"))
                    self.assertEqual(load_scenario(path), txs[node])

    def test_order_preserved(self):
        """P1-I5, P1-I7: le transazioni sono restituite nell'ordine del file."""
        path = self.write("ord.txt", b"d 1\n# x\n\np 2\nDEPOSITO 3 # c\n")
        self.assertEqual(load_scenario(path), [Transaction(DEPOSIT, 1), Transaction(WITHDRAWAL, 2),
                                               Transaction(DEPOSIT, 3)])

    def test_bom_and_crlf_accepted(self):
        """P1-I7: BOM UTF-8 e fine riga CRLF ammessi."""
        path = self.write("bom.txt", b"\xef\xbb\xbfdeposito 10\r\n# c\r\nprelievo 5\r\n")
        self.assertEqual(load_scenario(path), [Transaction(DEPOSIT, 10), Transaction(WITHDRAWAL, 5)])

    def test_empty_file(self):
        """P1-I7: file vuoto -> nessuna transazione."""
        self.assertEqual(load_scenario(self.write("vuoto.txt", b"")), [])

    def test_path_object_accepted(self):
        """P1-I7: load_scenario accetta anche un PathLike."""
        p = Path(self.write("p.txt", b"d 5\n"))
        self.assertEqual(load_scenario(p), [Transaction(DEPOSIT, 5)])

    def test_missing_file(self):
        """P1-I7: file inesistente -> ScenarioError(line_no=0, file_non_leggibile)."""
        path = str(self.tmp / "non_esiste.txt")
        with self.assertRaises(ScenarioError) as cm:
            load_scenario(path)
        e = cm.exception
        self.assertEqual((e.path, e.line_no, e.reason), (path, 0, "file_non_leggibile"))
        self.assertEqual(str(e), "file_non_leggibile")

    def test_directory_is_unreadable(self):
        """P1-I7: una cartella al posto del file -> file_non_leggibile (OSError)."""
        with self.assertRaises(ScenarioError) as cm:
            load_scenario(str(self.tmp))
        self.assertEqual((cm.exception.line_no, cm.exception.reason), (0, "file_non_leggibile"))

    def test_invalid_encoding(self):
        """P1-I7: byte non UTF-8 -> codifica_non_valida, line_no=0."""
        path = self.write("enc.txt", b"deposito 10\n\xe9\xff\n")
        with self.assertRaises(ScenarioError) as cm:
            load_scenario(path)
        self.assertEqual((cm.exception.line_no, cm.exception.reason), (0, "codifica_non_valida"))

    def test_error_line_number(self):
        """P1-I7: errore alla riga 3 -> line_no=3; si ferma al primo errore."""
        path = self.write("err.txt", b"# c\ndeposito 10\nPRELIEVO -5\nversa 1\n")
        with self.assertRaises(ScenarioError) as cm:
            load_scenario(path)
        e = cm.exception
        self.assertEqual((e.line_no, e.reason), (3, "importo_non_valido"))
        self.assertEqual(str(e), "riga 3: importo_non_valido")

    def test_each_reason_reported(self):
        """P1-I7: ogni motivo di ParseError viene riportato nello ScenarioError."""
        for text, reason in (("versa 1", "comando_sconosciuto"), ("deposito", "importo_mancante"),
                             ("deposito 0", "importo_non_valido"), ("d 1 2", "troppi_argomenti")):
            with self.subTest(text=text):
                path = self.write("r.txt", (text + "\n").encode())
                with self.assertRaises(ScenarioError) as cm:
                    load_scenario(path)
                self.assertEqual((cm.exception.line_no, cm.exception.reason), (1, reason))


class ScenarioFilesInRepositoryTest(unittest.TestCase):
    """I file consegnati in ``scenari/`` hanno il contenuto esatto atteso."""

    def check_dir(self, sub: str, files: dict[str, str], txs: dict[str, list]):
        for node, content in files.items():
            path = S.ROOT / "scenari" / sub / f"{node}.txt"
            with self.subTest(file=str(path)):
                self.assertTrue(path.is_file(), f"file mancante: {path}")
                self.assertEqual(path.read_bytes(), content.encode("ascii"),
                                 f"contenuto di {path} diverso dal design (ASCII, \\n, newline finale)")
                self.assertEqual(load_scenario(str(path)), txs[node])

    def test_reference_files(self):
        """P1-19, P1-I7: scenari/riferimento/ATM1..4.txt con il contenuto atteso."""
        self.check_dir("riferimento", REFERENCE_FILES, REFERENCE_TX)

    def test_refusal_files(self):
        """P1-I2, P1-I7: scenari/rifiuto/ATM1..4.txt con il contenuto atteso."""
        # Si assume per questi file la stessa regola degli scenari di riferimento: ASCII senza BOM,
        # fine riga \n, newline finale.
        self.check_dir("rifiuto", REFUSAL_FILES, REFUSAL_TX)


if __name__ == "__main__":
    unittest.main()
