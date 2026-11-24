"""Test unitari di ``tokenring/protocol.py``."""
from __future__ import annotations

import dataclasses
import json
import unittest

import support  # noqa: F401  (mette token-ring-atm in sys.path)
from tokenring.protocol import (
    MAX_LINE_BYTES, MSG_TYPE_TOKEN, NODE_ID_PATTERN, ProtocolError, Token,
    create_initial_token, decode_message, encode_token, is_valid_node_id, make_forward_token,
)

VALID_IDS = ("ATM1", "ATM2", "ATM3", "ATM4")
INVALID_IDS = ("ATM0", "ATM5", "atm1", "", " ATM1", "ATM1 ", "ATM12", "ATM1\n", "ATM")


def _line(**overrides) -> bytes:
    d = {"type": "TOKEN", "seq": 1, "balance": 1000, "sender": "ATM1", "receiver": "ATM2"}
    d.update(overrides)
    return json.dumps(d).encode("utf-8") + b"\n"


class ProtocolConstantsTest(unittest.TestCase):
    def test_constants(self):
        """P1-04: costanti del protocollo (tipo TOKEN, riga massima 4096 byte, ID ATM1..ATM4)."""
        self.assertEqual(MSG_TYPE_TOKEN, "TOKEN")
        self.assertEqual(MAX_LINE_BYTES, 4096)
        self.assertEqual(NODE_ID_PATTERN, r"ATM[1-4]")

    def test_protocol_error_is_value_error(self):
        """P1-04: ProtocolError e' una sottoclasse di ValueError."""
        self.assertTrue(issubclass(ProtocolError, ValueError))


class TokenTest(unittest.TestCase):
    def test_valid_token(self):
        """P1-I1: il token porta seq e saldo."""
        t = Token(seq=1, balance=1000, sender="ATM1", receiver="ATM2")
        self.assertEqual((t.seq, t.balance, t.sender, t.receiver), (1, 1000, "ATM1", "ATM2"))
        self.assertEqual(Token(0, 0, "ATM4", "ATM4").balance, 0)

    def test_token_is_immutable(self):
        """P1-I1, P1-14: il token e' immutabile (dataclass frozen)."""
        t = Token(1, 1000, "ATM1", "ATM2")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            t.balance = 0  # type: ignore[misc]

    def test_invalid_numeric_fields(self):
        """P1-I1: seq/balance negativi, bool o float -> ValueError."""
        for seq, bal in ((-1, 1000), (1, -1), (True, 1000), (1, False), (1.0, 1000), (1, 1000.0),
                         ("1", 1000), (1, None)):
            with self.subTest(seq=seq, balance=bal):
                with self.assertRaises(ValueError):
                    Token(seq, bal, "ATM1", "ATM2")  # type: ignore[arg-type]

    def test_invalid_node_ids(self):
        """P1-01, P1-09: ID diversi da ATM1..ATM4 -> ValueError."""
        for bad in INVALID_IDS + (1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Token(1, 1000, bad, "ATM2")  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    Token(1, 1000, "ATM1", bad)  # type: ignore[arg-type]

    def test_is_valid_node_id(self):
        """P1-01: is_valid_node_id accetta solo ATM1..ATM4 come str (fullmatch)."""
        for good in VALID_IDS:
            self.assertTrue(is_valid_node_id(good), good)
        for bad in INVALID_IDS + (1, None, b"ATM1"):
            self.assertFalse(is_valid_node_id(bad), repr(bad))


class TokenFactoriesTest(unittest.TestCase):
    def test_create_initial_token(self):
        """P1-18: token creato con seq=0, sender == receiver == creatore, saldo iniziale."""
        self.assertEqual(create_initial_token("ATM1", 1000), Token(0, 1000, "ATM1", "ATM1"))
        self.assertEqual(create_initial_token("ATM3", 0), Token(0, 0, "ATM3", "ATM3"))

    def test_create_initial_token_invalid(self):
        """P1-18: saldo negativo/non intero o ID non valido -> ValueError."""
        for node, bal in (("ATM1", -1), ("ATM1", True), ("ATM1", 10.0), ("ATM5", 1000), ("", 1000)):
            with self.subTest(node=node, balance=bal):
                with self.assertRaises(ValueError):
                    create_initial_token(node, bal)  # type: ignore[arg-type]

    def test_make_forward_token(self):
        """P1-I1, P1-09: inoltro = seq+1, saldo invariato, nuovi mittente/destinatario."""
        t = Token(3, 900, "ATM3", "ATM4")
        f = make_forward_token(t, "ATM4", "ATM1")
        self.assertEqual(f, Token(4, 900, "ATM4", "ATM1"))
        self.assertEqual(t, Token(3, 900, "ATM3", "ATM4"), "il token originale non va modificato")

    def test_make_forward_token_invalid(self):
        """P1-09: mittente o destinatario non validi -> ValueError."""
        t = Token(0, 1000, "ATM1", "ATM1")
        with self.assertRaises(ValueError):
            make_forward_token(t, "ATM1", "ATM5")
        with self.assertRaises(ValueError):
            make_forward_token(t, "atm1", "ATM2")


class EncodeTokenTest(unittest.TestCase):
    def test_exact_bytes(self):
        """P1-04: JSON compatto + newline, ordine delle chiavi fisso."""
        self.assertEqual(
            encode_token(Token(1, 1000, "ATM1", "ATM2")),
            b'{"type":"TOKEN","seq":1,"balance":1000,"sender":"ATM1","receiver":"ATM2"}\n')
        self.assertEqual(
            encode_token(Token(4, 400, "ATM4", "ATM1")),
            b'{"type":"TOKEN","seq":4,"balance":400,"sender":"ATM4","receiver":"ATM1"}\n')

    def test_single_trailing_newline(self):
        """P1-04: un messaggio = una riga terminata da un solo \\n, ASCII puro."""
        data = encode_token(Token(12, 0, "ATM2", "ATM3"))
        self.assertTrue(data.endswith(b"\n"))
        self.assertFalse(data.endswith(b"\n\n"))
        self.assertEqual(data.count(b"\n"), 1)
        data.decode("ascii")

    def test_roundtrip(self):
        """P1-I1: decode_message(encode_token(t)) == t."""
        for t in (Token(0, 0, "ATM1", "ATM1"), Token(1, 1000, "ATM1", "ATM2"),
                  Token(123456, 1_000_000_000, "ATM4", "ATM1")):
            with self.subTest(t=t):
                self.assertEqual(decode_message(encode_token(t)), t)


class DecodeMessageTest(unittest.TestCase):
    def test_valid_line_endings(self):
        """P1-04: riga con \\n, con \\r\\n o senza terminatore e' accettata."""
        body = b'{"type":"TOKEN","seq":1,"balance":1000,"sender":"ATM1","receiver":"ATM2"}'
        expected = Token(1, 1000, "ATM1", "ATM2")
        for line in (body + b"\n", body + b"\r\n", body, b"  " + body + b"  \n"):
            with self.subTest(line=line):
                self.assertEqual(decode_message(line), expected)

    def test_key_order_irrelevant(self):
        """P1-04: un oggetto JSON con le chiavi in altro ordine e' valido."""
        line = b'{"receiver":"ATM2","sender":"ATM1","balance":5,"seq":7,"type":"TOKEN"}\n'
        self.assertEqual(decode_message(line), Token(7, 5, "ATM1", "ATM2"))

    def test_malformed_cases(self):
        """P1-04, P1-14: i 7 casi di messaggio malformato (UTF-8, JSON, non oggetto, chiavi,
        tipo, interi, ID) sollevano ProtocolError con testo non vuoto."""
        cases = {
            "1 utf-8 non valido": b'\xff\xfe{"type":"TOKEN"}\n',
            "2 json non valido": b"questo non e' json\n",
            "2 riga vuota": b"   \n",
            "3 non oggetto (lista)": b"[1, 2, 3]\n",
            "3 non oggetto (null)": b"null\n",
            "3 non oggetto (numero)": b"42\n",
            "4 chiave mancante": b'{"type":"TOKEN","seq":1,"balance":1000,"sender":"ATM1"}\n',
            "4 chiave in piu'": _line(extra=1),
            "5 type diverso": _line(type="ACK"),
            "5 type minuscolo": _line(type="token"),
            "6 seq negativo": _line(seq=-1),
            "6 seq booleano": _line(seq=True),
            "6 seq decimale": _line(seq=1.5),
            "6 seq stringa": _line(seq="1"),
            "6 balance decimale": _line(balance=1000.0),
            "6 balance negativo": _line(balance=-5),
            "6 balance booleano": _line(balance=False),
            "6 balance null": _line(balance=None),
            "7 sender non valido": _line(sender="ATM5"),
            "7 receiver minuscolo": _line(receiver="atm2"),
            "7 receiver numerico": _line(receiver=2),
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(ProtocolError) as cm:
                    decode_message(data)
                self.assertTrue(str(cm.exception).strip(), "il messaggio d'errore e' vuoto")


if __name__ == "__main__":
    unittest.main()
