"""Test unitari dei comandi da tastiera: ``parse_command``."""
from __future__ import annotations

import unittest

import support  # noqa: F401
from tokenring.transactions import (
    CMD_HELP, CMD_OPERATION, CMD_QUIT, CMD_STATUS, DEPOSIT, WITHDRAWAL, Command, ParseError,
    Transaction, parse_command,
)


class CommandConstantsTest(unittest.TestCase):
    def test_constants(self):
        """P1-I7: costanti dei tipi di comando."""
        self.assertEqual((CMD_OPERATION, CMD_STATUS, CMD_HELP, CMD_QUIT),
                         ("operation", "status", "help", "quit"))
        self.assertIsNone(Command(CMD_STATUS).transaction)


class ParseCommandTest(unittest.TestCase):
    def test_operations_with_aliases(self):
        """P1-07, P1-I7: deposito/prelievo con alias e maiuscole indifferenti."""
        cases = {
            "deposito 50": Transaction(DEPOSIT, 50), "d 50": Transaction(DEPOSIT, 50),
            "D 50": Transaction(DEPOSIT, 50), "DEPOSITO 50": Transaction(DEPOSIT, 50),
            "prelievo 20": Transaction(WITHDRAWAL, 20), "p 20": Transaction(WITHDRAWAL, 20),
            "P 20": Transaction(WITHDRAWAL, 20), "Prelievo 20": Transaction(WITHDRAWAL, 20),
            "  d   50  # nota\n": Transaction(DEPOSIT, 50),
        }
        for line, tx in cases.items():
            with self.subTest(line=line):
                self.assertEqual(parse_command(line), Command(CMD_OPERATION, tx))

    def test_control_commands_with_aliases(self):
        """P1-I7: stato/s, aiuto/h/?, esci/q (maiuscole indifferenti)."""
        cases = {
            CMD_STATUS: ("stato", "s", "STATO", "S", "Stato", "stato # c", "  s  \n"),
            CMD_HELP: ("aiuto", "h", "?", "AIUTO", "H", "? # c"),
            CMD_QUIT: ("esci", "q", "ESCI", "Q", "esci\r\n"),
        }
        for kind, lines in cases.items():
            for line in lines:
                with self.subTest(line=line):
                    self.assertEqual(parse_command(line), Command(kind, None))

    def test_empty_and_comment(self):
        """P1-I7: riga vuota o solo commento -> None (nessun log)."""
        for line in ("", "   ", "\n", "# solo commento", "  # c\n"):
            with self.subTest(line=line):
                self.assertIsNone(parse_command(line))

    def test_errors(self):
        """P1-I7: input non valido -> ParseError con il motivo corretto, mai crash."""
        cases = {
            "stato extra": "troppi_argomenti",
            "s 5": "troppi_argomenti",
            "aiuto x": "troppi_argomenti",
            "? x": "troppi_argomenti",
            "esci ora": "troppi_argomenti",
            "q 1": "troppi_argomenti",
            "versa 10": "comando_sconosciuto",
            "x": "comando_sconosciuto",
            "p": "importo_mancante",
            "deposito": "importo_mancante",
            "d -5": "importo_non_valido",
            "d 0": "importo_non_valido",
            "d dieci": "importo_non_valido",
            "d 10 20": "troppi_argomenti",
        }
        for line, reason in cases.items():
            with self.subTest(line=line):
                with self.assertRaises(ParseError) as cm:
                    parse_command(line)
                self.assertEqual(cm.exception.reason, reason)

    def test_error_text_keeps_comment_and_spaces(self):
        """P1-I7: ParseError.text = riga senza \\r\\n finali, con spazi e commento."""
        with self.assertRaises(ParseError) as cm:
            parse_command("  versa 10  # x\n")
        self.assertEqual(cm.exception.text, "  versa 10  # x")
        self.assertEqual(str(cm.exception), "comando_sconosciuto")


if __name__ == "__main__":
    unittest.main()
