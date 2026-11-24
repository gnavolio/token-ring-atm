"""Test unitari di ``tokenring/logfmt.py``.

Gli attesi sono l'elenco chiuso dei 24 eventi (sotto), le regole di formattazione e le righe
d'esempio di ``golden_logs.py``; le righe prodotte sono verificate con le regex ufficiali di
``support.py``.
"""
from __future__ import annotations

import io
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

import support as S
from golden_logs import EXAMPLE_LINES
from tokenring.logfmt import (
    ALARM_SGR, EVENT_COLUMN_WIDTH, EVENT_OFFSET, EVENTS, FIELDS_OFFSET, MARK_OFFSET,
    MAX_QUOTED_LEN, NODE_COLORS, QUOTED_FIELDS, SECTION_SGR, STYLE_ALARM, STYLE_NORMAL,
    STYLE_SECTION, NodeLogger, colorize, format_log_line, format_timestamp, format_value,
)

N, SEC, AL = "normale", "sezione", "allarme"
# Elenco chiuso dei 24 eventi di log: (codice, marca, campi, stile), nell'ordine ufficiale.
EXPECTED_EVENTS = [
    ("NODO_AVVIATO", "   ", ("successore", "porta_successore", "ritardo"), N),
    ("SCENARIO_CARICATO", "   ", ("transazioni",), N),
    ("IN_ASCOLTO", "   ", ("porta",), N),
    ("CONNESSIONE_ATTESA", "   ", ("successore", "porta_successore", "tentativo"), N),
    ("CONNESSIONE_OK", "   ", ("successore", "porta_successore", "tentativi"), N),
    ("PREDECESSORE_CONNESSO", "   ", ("remoto",), N),
    ("PREDECESSORE_CHIUSO", "   ", ("remoto",), N),
    ("SUCCESSORE_PERSO", "!!!", ("successore", "porta_successore"), AL),
    ("TOKEN_CREATO", "   ", ("seq", "saldo"), N),
    ("TOKEN_RICEVUTO", "   ", ("seq", "saldo", "da"), N),
    ("NESSUNA_TRANSAZIONE", "   ", ("seq", "saldo"), N),
    ("INIZIO_TRANSAZIONE", ">>>", ("seq", "saldo", "op", "importo"), SEC),
    ("SALDO_AGGIORNATO", "===", ("seq", "saldo", "prima", "variazione"), SEC),
    ("TRANSAZIONE_RIFIUTATA", "!!!", ("seq", "saldo", "motivo"), AL),
    ("FINE_TRANSAZIONE", "<<<", ("seq", "saldo", "esito"), SEC),
    ("TOKEN_INOLTRATO", "   ", ("seq", "saldo", "a"), N),
    ("TOKEN_RITIRATO", "   ", ("seq", "saldo", "giri"), N),
    ("TOKEN_PERSO", "!!!", ("seq", "saldo"), AL),
    ("COMANDO_ACCETTATO", "   ", ("op", "importo", "in_coda"), N),
    ("COMANDO_NON_VALIDO", "!!!", ("motivo", "testo"), AL),
    ("STATO", "   ", ("token", "in_coda", "ultimo_seq", "saldo_visto"), N),
    ("AIUTO", "   ", ("uso",), N),
    ("ERRORE", "!!!", ("tipo", "dettaglio"), AL),
    ("ARRESTO", "   ", ("motivo", "codice"), N),
]
USO = "deposito N | prelievo N | stato | aiuto | esci"

# Valori validi per ogni evento (tipi come li passa il nodo: variazione e' una stringa con segno).
SAMPLE_FIELDS = {
    "NODO_AVVIATO": {"successore": "ATM2", "porta_successore": 5002, "ritardo": 1.5},
    "SCENARIO_CARICATO": {"transazioni": 1},
    "IN_ASCOLTO": {"porta": 5001},
    "CONNESSIONE_ATTESA": {"successore": "ATM1", "porta_successore": 5001, "tentativo": 1},
    "CONNESSIONE_OK": {"successore": "ATM1", "porta_successore": 47101, "tentativi": 3},
    "PREDECESSORE_CONNESSO": {"remoto": "127.0.0.1:51001"},
    "PREDECESSORE_CHIUSO": {"remoto": "127.0.0.1:51002"},
    "SUCCESSORE_PERSO": {"successore": "ATM3", "porta_successore": 5003},
    "TOKEN_CREATO": {"seq": 0, "saldo": 1000},
    "TOKEN_RICEVUTO": {"seq": 1, "saldo": 1000, "da": "ATM1"},
    "NESSUNA_TRANSAZIONE": {"seq": 0, "saldo": 1000},
    "INIZIO_TRANSAZIONE": {"seq": 1, "saldo": 1000, "op": "PRELIEVO", "importo": 200},
    "SALDO_AGGIORNATO": {"seq": 1, "saldo": 800, "prima": 1000, "variazione": "-200"},
    "TRANSAZIONE_RIFIUTATA": {"seq": 1, "saldo": 1000, "motivo": "saldo_insufficiente"},
    "FINE_TRANSAZIONE": {"seq": 1, "saldo": 800, "esito": "ACCETTATA"},
    "TOKEN_INOLTRATO": {"seq": 2, "saldo": 800, "a": "ATM3"},
    "TOKEN_RITIRATO": {"seq": 4, "saldo": 400, "giri": 1},
    "TOKEN_PERSO": {"seq": 3, "saldo": 900},
    "COMANDO_ACCETTATO": {"op": "DEPOSITO", "importo": 50, "in_coda": 0},
    "COMANDO_NON_VALIDO": {"motivo": "comando_sconosciuto", "testo": "versa 100"},
    "STATO": {"token": False, "in_coda": 1, "ultimo_seq": None, "saldo_visto": None},
    "AIUTO": {"uso": USO},
    "ERRORE": {"tipo": "porta_occupata",
               "dettaglio": "porta 5002 occupata o non utilizzabile (10048)"},
    "ARRESTO": {"motivo": "giri_completati", "codice": 0},
}
TS = datetime(2026, 1, 1, 10, 0, 4, 708999)


def example(prefix: str) -> str:
    """La riga d'esempio di ``golden_logs.EXAMPLE_LINES`` che inizia con ``prefix`` (unica)."""
    found = [ln for ln in EXAMPLE_LINES if ln.startswith(prefix)]
    assert len(found) >= 1, prefix
    return found[0]


class LogConstantsTest(unittest.TestCase):
    def test_column_constants(self):
        """P1-20, P1-I8: colonne fisse della riga."""
        self.assertEqual((MARK_OFFSET, EVENT_OFFSET, EVENT_COLUMN_WIDTH, FIELDS_OFFSET,
                          MAX_QUOTED_LEN), (20, 24, 21, 46, 100))

    def test_style_and_color_constants(self):
        """P1-I6: stili, colori per nodo, video inverso, allarme."""
        self.assertEqual((STYLE_NORMAL, STYLE_SECTION, STYLE_ALARM), ("normale", "sezione", "allarme"))
        self.assertEqual(QUOTED_FIELDS, frozenset({"testo", "dettaglio", "uso"}))
        self.assertEqual(dict(NODE_COLORS), {"ATM1": "96", "ATM2": "92", "ATM3": "93", "ATM4": "95"})
        self.assertEqual(dict(SECTION_SGR),
                         {"ATM1": "7;96", "ATM2": "7;92", "ATM3": "7;93", "ATM4": "7;95"})
        self.assertEqual(ALARM_SGR, "1;91")

    def test_events_table(self):
        """P1-20: EVENTS = esattamente i 24 codici dell'elenco ufficiale, nello stesso ordine, con marca,
        campi e stile della tabella."""
        self.assertEqual(list(EVENTS), [e[0] for e in EXPECTED_EVENTS])
        for code, mark, fields, style in EXPECTED_EVENTS:
            with self.subTest(event=code):
                spec = EVENTS[code]
                self.assertEqual(spec.mark, mark)
                self.assertEqual(tuple(spec.fields), fields)
                self.assertEqual(spec.style, style)

    def test_events_match_official_regex_table(self):
        """P1-20: le marche di EVENTS coincidono con EVENT_MARK delle regex ufficiali."""
        self.assertEqual({k: v.mark for k, v in EVENTS.items()}, S.EVENT_MARK)


class FormatTimestampTest(unittest.TestCase):
    def test_truncated_milliseconds(self):
        """P1-I8: HH:MM:SS.mmm con millisecondi troncati, non arrotondati."""
        cases = {
            datetime(2026, 1, 1, 9, 5, 7, 123999): "09:05:07.123",
            datetime(2026, 1, 1, 23, 59, 59, 999999): "23:59:59.999",
            datetime(2026, 1, 1, 0, 0, 0, 0): "00:00:00.000",
            datetime(2026, 1, 1, 12, 0, 0, 999): "12:00:00.000",
            datetime(2026, 1, 1, 12, 0, 0, 1000): "12:00:00.001",
        }
        for ts, expected in cases.items():
            with self.subTest(ts=ts):
                self.assertEqual(format_timestamp(ts), expected)


class FormatValueTest(unittest.TestCase):
    def test_scalars(self):
        """P1-20: bool -> si/no, int, float senza zeri finali, None -> '-'."""
        cases = [
            (("token", True), "si"), (("token", False), "no"),
            (("variazione", -200), "-200"), (("saldo", 0), "0"), (("saldo", 1000), "1000"),
            (("ritardo", 1.5), "1.5"), (("ritardo", 0.1), "0.1"), (("ritardo", 0.0), "0"),
            (("ritardo", 2.0), "2"), (("ritardo", 60.0), "60"), (("ritardo", 1.25), "1.25"),
            (("ritardo", 0.125), "0.125"),
            (("ultimo_seq", None), "-"),
        ]
        for (key, value), expected in cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(format_value(key, value), expected)

    def test_strings(self):
        """P1-20: stringhe semplici senza virgolette; spazi, '"', non ASCII e campi testuali
        tra virgolette, con sostituzioni."""
        cases = [
            (("a", "ATM1"), "ATM1"),
            (("variazione", "+100"), "+100"),
            (("variazione", "-200"), "-200"),
            (("remoto", "127.0.0.1:51001"), "127.0.0.1:51001"),
            (("op", "DEPOSITO"), "DEPOSITO"),
            (("motivo", "saldo_insufficiente"), "saldo_insufficiente"),
            (("motivo", "a b"), '"a b"'),
            (("motivo", ""), '""'),
            (("testo", "abc"), '"abc"'),
            (("testo", ""), '""'),
            (("testo", "versa 100"), '"versa 100"'),
            (("testo", 'di "x"'), "\"di 'x'\""),
            (("testo", "caffè # nota"), '"caff? # nota"'),
            (("dettaglio", "a\tb"), '"a?b"'),
            (("uso", USO), '"' + USO + '"'),
            (("motivo", "a=b"), '"a=b"'),
        ]
        for (key, value), expected in cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(format_value(key, value), expected)

    def test_truncation(self):
        """P1-20: contenuto oltre 100 caratteri -> primi 97 + '...'."""
        self.assertEqual(format_value("testo", "a" * 150), '"' + "a" * 97 + '..."')
        self.assertEqual(format_value("testo", "b" * 100), '"' + "b" * 100 + '"')
        self.assertEqual(format_value("testo", "c" * 101), '"' + "c" * 97 + '..."')
        # la sostituzione avviene prima del troncamento
        self.assertEqual(format_value("dettaglio", '"' * 120), '"' + "'" * 97 + '..."')

    def test_invalid_types(self):
        """P1-20: tipi non ammessi -> ValueError."""
        for value in ([1], {"a": 1}, b"x", object(), (1, 2)):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    format_value("saldo", value)


class FormatLogLineTest(unittest.TestCase):
    def test_every_event_matches_official_regex(self):
        """P1-20, P1-I8: per ogni evento la riga soddisfa LINE_RE, FIELDS_RE, EVENT_MARK, colonne
        fisse; timestamp con millisecondi; nessun '\\n' ne' spazio finale."""
        self.assertEqual(set(SAMPLE_FIELDS), set(EVENTS))
        for event, fields in SAMPLE_FIELDS.items():
            for node in S.RING:
                with self.subTest(event=event, node=node):
                    line = format_log_line(TS, node, event, fields)
                    entry, errors = S.check_line(node, 0, line)
                    self.assertEqual(errors, [], line)
                    self.assertEqual(entry.event, event)
                    self.assertTrue(line.startswith("10:00:04.708 [" + node + "] "))
                    self.assertEqual(line[20:23], EVENTS[event].mark)
                    self.assertEqual(line[24:45].rstrip(), event)
                    first = EVENTS[event].fields[0]
                    self.assertTrue(line[46:].startswith(first + "="), line)
                    self.assertNotIn("\n", line)

    def test_exact_lines_from_design_examples(self):
        """P1-20, P1-I8: righe identiche alle righe d'esempio di riferimento."""
        D = datetime
        cases = [
            (D(2026, 1, 1, 10, 0, 0, 101500), "ATM4", "NODO_AVVIATO",
             {"successore": "ATM1", "porta_successore": 5001, "ritardo": 1.5},
             "10:00:00.101 [ATM4]     NODO_AVVIATO"),
            (D(2026, 1, 1, 11, 0, 0, 990000), "ATM1", "NODO_AVVIATO",
             {"successore": "ATM2", "porta_successore": 47112, "ritardo": 0.1},
             "11:00:00.990 [ATM1]     NODO_AVVIATO"),
            (D(2026, 1, 1, 10, 0, 4, 708999), "ATM2", "TOKEN_RICEVUTO",
             {"seq": 1, "saldo": 1000, "da": "ATM1"}, "10:00:04.708 [ATM2]     TOKEN_RICEVUTO"),
            (D(2026, 1, 1, 10, 0, 4, 708000), "ATM2", "INIZIO_TRANSAZIONE",
             {"seq": 1, "saldo": 1000, "op": "PRELIEVO", "importo": 200},
             "10:00:04.708 [ATM2] >>> INIZIO_TRANSAZIONE"),
            (D(2026, 1, 1, 10, 0, 4, 709000), "ATM2", "SALDO_AGGIORNATO",
             {"seq": 1, "saldo": 800, "prima": 1000, "variazione": "-200"},
             "10:00:04.709 [ATM2] === SALDO_AGGIORNATO"),
            (D(2026, 1, 1, 10, 0, 6, 212000), "ATM3", "SALDO_AGGIORNATO",
             {"seq": 2, "saldo": 900, "prima": 800, "variazione": "+100"},
             "10:00:06.212 [ATM3] === SALDO_AGGIORNATO"),
            (D(2026, 1, 1, 11, 0, 1, 97000), "ATM2", "TRANSAZIONE_RIFIUTATA",
             {"seq": 1, "saldo": 1000, "motivo": "saldo_insufficiente"},
             "11:00:01.097 [ATM2] !!! TRANSAZIONE_RIFIUTATA"),
            (D(2026, 1, 1, 11, 0, 1, 98000), "ATM2", "FINE_TRANSAZIONE",
             {"seq": 1, "saldo": 1000, "esito": "RIFIUTATA"},
             "11:00:01.098 [ATM2] <<< FINE_TRANSAZIONE"),
            (D(2026, 1, 1, 10, 0, 4, 707000), "ATM1", "TOKEN_INOLTRATO",
             {"seq": 1, "saldo": 1000, "a": "ATM2"}, "10:00:04.707 [ATM1]     TOKEN_INOLTRATO"),
            (D(2026, 1, 1, 11, 0, 1, 700000), "ATM1", "TOKEN_RITIRATO",
             {"seq": 4, "saldo": 0, "giri": 1}, "11:00:01.700 [ATM1]     TOKEN_RITIRATO"),
            (D(2026, 1, 1, 10, 8, 1, 455000), "ATM3", "TOKEN_PERSO",
             {"seq": 37, "saldo": 900}, "10:08:01.455 [ATM3] !!! TOKEN_PERSO"),
            (D(2026, 1, 1, 10, 0, 1, 215000), "ATM4", "PREDECESSORE_CONNESSO",
             {"remoto": "127.0.0.1:51001"}, "10:00:01.215 [ATM4]     PREDECESSORE_CONNESSO"),
            (D(2026, 1, 1, 10, 5, 14, 2000), "ATM2", "SUCCESSORE_PERSO",
             {"successore": "ATM3", "porta_successore": 5003},
             "10:05:14.002 [ATM2] !!! SUCCESSORE_PERSO"),
            (D(2026, 1, 1, 10, 0, 3, 612000), "ATM4", "CONNESSIONE_OK",
             {"successore": "ATM1", "porta_successore": 5001, "tentativi": 3},
             "10:00:03.612 [ATM4]     CONNESSIONE_OK"),
            (D(2026, 1, 1, 10, 1, 4, 500000), "ATM2", "COMANDO_ACCETTATO",
             {"op": "DEPOSITO", "importo": 10, "in_coda": 0},
             "10:01:04.500 [ATM2]     COMANDO_ACCETTATO"),
            (D(2026, 1, 1, 10, 1, 20, 100000), "ATM2", "COMANDO_NON_VALIDO",
             {"motivo": "comando_sconosciuto", "testo": "versa 100"},
             "10:01:20.100 [ATM2] !!! COMANDO_NON_VALIDO"),
            (D(2026, 1, 1, 10, 0, 0, 500000), "ATM4", "STATO",
             {"token": False, "in_coda": 1, "ultimo_seq": None, "saldo_visto": None},
             "10:00:00.500 [ATM4]     STATO"),
            (D(2026, 1, 1, 10, 1, 30, 0), "ATM3", "STATO",
             {"token": False, "in_coda": 1, "ultimo_seq": 22, "saldo_visto": 400},
             "10:01:30.000 [ATM3]     STATO"),
            (D(2026, 1, 1, 10, 1, 40, 123000), "ATM1", "AIUTO", {"uso": USO},
             "10:01:40.123 [ATM1]     AIUTO"),
            (D(2026, 1, 1, 10, 0, 0, 104000), "ATM2", "ERRORE",
             {"tipo": "porta_occupata", "dettaglio": "porta 5002 occupata o non utilizzabile (10048)"},
             "10:00:00.104 [ATM2] !!! ERRORE"),
            (D(2026, 1, 1, 10, 0, 0, 105000), "ATM2", "ARRESTO",
             {"motivo": "errore", "codice": 3}, "10:00:00.105 [ATM2]     ARRESTO"),
            (D(2026, 1, 1, 10, 0, 3, 206000), "ATM1", "TOKEN_CREATO",
             {"seq": 0, "saldo": 1000}, "10:00:03.206 [ATM1]     TOKEN_CREATO"),
        ]
        for ts, node, event, fields, prefix in cases:
            with self.subTest(prefix=prefix):
                self.assertEqual(format_log_line(ts, node, event, fields), example(prefix))

    def test_field_order_irrelevant(self):
        """P1-20: l'ordine delle chiavi nel dizionario non cambia la riga."""
        a = format_log_line(TS, "ATM2", "SALDO_AGGIORNATO",
                            {"seq": 1, "saldo": 800, "prima": 1000, "variazione": "-200"})
        b = format_log_line(TS, "ATM2", "SALDO_AGGIORNATO",
                            {"variazione": "-200", "prima": 1000, "saldo": 800, "seq": 1})
        self.assertEqual(a, b)

    def test_invalid_calls(self):
        """P1-20: evento sconosciuto, nodo non valido, campi mancanti o in piu' -> ValueError."""
        bad = [
            ("ATM1", "EVENTO_INVENTATO", {"seq": 1}),
            ("ATM5", "TOKEN_CREATO", {"seq": 0, "saldo": 1000}),
            ("atm1", "TOKEN_CREATO", {"seq": 0, "saldo": 1000}),
            ("ATM1", "TOKEN_CREATO", {"seq": 0}),
            ("ATM1", "TOKEN_CREATO", {"seq": 0, "saldo": 1000, "extra": 1}),
            ("ATM1", "ARRESTO", {}),
        ]
        for node, event, fields in bad:
            with self.subTest(node=node, event=event, fields=fields):
                with self.assertRaises(ValueError):
                    format_log_line(TS, node, event, fields)


class ColorizeTest(unittest.TestCase):
    def test_three_styles_exact_bytes(self):
        """P1-I6: colore del nodo, video inverso in sezione critica, rosso per gli allarmi."""
        line = "x"
        self.assertEqual(colorize(line, "ATM2", "SALDO_AGGIORNATO"), "\x1b[7;92mx\x1b[0m")
        self.assertEqual(colorize(line, "ATM1", "TOKEN_RICEVUTO"), "\x1b[96mx\x1b[0m")
        self.assertEqual(colorize(line, "ATM4", "TOKEN_PERSO"), "\x1b[1;91mx\x1b[0m")

    def test_all_events_all_nodes(self):
        """P1-I6: ogni evento riceve l'SGR del proprio stile per ogni nodo."""
        normal = {"ATM1": "96", "ATM2": "92", "ATM3": "93", "ATM4": "95"}
        for code, _, _, style in EXPECTED_EVENTS:
            for node in S.RING:
                sgr = {"normale": normal[node], "sezione": "7;" + normal[node],
                       "allarme": "1;91"}[style]
                with self.subTest(event=code, node=node):
                    self.assertEqual(colorize("riga", node, code), f"\x1b[{sgr}mriga\x1b[0m")

    def test_invalid(self):
        """P1-I6: evento o nodo non validi -> ValueError."""
        with self.assertRaises(ValueError):
            colorize("x", "ATM1", "NON_ESISTE")
        with self.assertRaises(ValueError):
            colorize("x", "ATM9", "ARRESTO")


class _BrokenStream:
    def write(self, s):
        raise OSError("stream rotto")

    def flush(self):
        raise OSError("stream rotto")


class NodeLoggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tokenring_unit_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = lambda: datetime(2026, 1, 1, 10, 0, 4, 708123)

    def make(self, path=None, use_color=False, stream=None) -> NodeLogger:
        path = path or self.tmp / "ATM2.log"
        logger = NodeLogger("ATM2", path, use_color, stream=stream if stream is not None
                            else io.StringIO(), clock=self.clock)
        self.addCleanup(logger.close)
        return logger

    def test_same_line_on_file_and_stream(self):
        """P1-20, P1-I8: stessa riga su file e su stream (senza colori), \\n come fine riga."""
        stream = io.StringIO()
        logger = self.make(stream=stream)
        line = logger.log("TOKEN_RICEVUTO", seq=1, saldo=1000, da="ATM1")
        logger.close()
        self.assertEqual(line, "10:00:04.708 [ATM2]     TOKEN_RICEVUTO        seq=1 saldo=1000 da=ATM1")
        self.assertEqual((self.tmp / "ATM2.log").read_bytes(), (line + "\n").encode("utf-8"))
        self.assertEqual(stream.getvalue(), line + "\n")

    def test_colors_only_on_stream(self):
        """P1-I6, P1-I8: con use_color lo stream ha i codici ANSI, il file no."""
        stream = io.StringIO()
        logger = self.make(use_color=True, stream=stream)
        line = logger.log("INIZIO_TRANSAZIONE", seq=1, saldo=1000, op="PRELIEVO", importo=200)
        logger.close()
        self.assertEqual(stream.getvalue(), "\x1b[7;92m" + line + "\x1b[0m\n")
        data = (self.tmp / "ATM2.log").read_bytes()
        self.assertEqual(data, (line + "\n").encode())
        self.assertNotIn(b"\x1b", data)

    def test_creates_parent_and_truncates(self):
        """P1-I8: crea le cartelle mancanti e tronca il file a ogni nuova istanza."""
        path = self.tmp / "a" / "b" / "ATM2.log"
        first = self.make(path)
        first.log("ARRESTO", motivo="interruzione", codice=0)
        first.close()
        second = self.make(path)
        line = second.log("TOKEN_CREATO", seq=0, saldo=1000)
        second.close()
        self.assertEqual(path.read_text(encoding="utf-8"), line + "\n")

    def test_close_is_idempotent_and_stops_writing(self):
        """P1-20: dopo close() nessuna scrittura e log() restituisce ''."""
        stream = io.StringIO()
        logger = self.make(stream=stream)
        logger.log("TOKEN_CREATO", seq=0, saldo=1000)
        logger.close()
        logger.close()
        self.assertEqual(logger.log("TOKEN_CREATO", seq=0, saldo=1000), "")
        self.assertEqual(len((self.tmp / "ATM2.log").read_text().splitlines()), 1)
        self.assertEqual(len(stream.getvalue().splitlines()), 1)

    def test_log_final_is_last_line(self):
        """P1-20: log_final scrive e chiude; dopo, log()/log_final() -> ''."""
        logger = self.make()
        logger.log("TOKEN_CREATO", seq=0, saldo=1000)
        final = logger.log_final("ARRESTO", motivo="giri_completati", codice=0)
        self.assertTrue(final.endswith("ARRESTO               motivo=giri_completati codice=0"))
        self.assertEqual(logger.log("TOKEN_CREATO", seq=0, saldo=1000), "")
        self.assertEqual(logger.log_final("ARRESTO", motivo="errore", codice=1), "")
        lines = (self.tmp / "ATM2.log").read_text().splitlines()
        self.assertEqual(lines[-1], final)
        self.assertEqual(len(lines), 2)

    def test_log_final_last_line_with_concurrent_threads(self):
        """P1-20: con 2 thread che registrano in ciclo, nessuna riga dopo ARRESTO."""
        logger = self.make()
        stop = threading.Event()

        def spam():
            while not stop.is_set():
                logger.log("STATO", token=False, in_coda=0, ultimo_seq=None, saldo_visto=None)

        threads = [threading.Thread(target=spam, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        logger.log_final("ARRESTO", motivo="interruzione", codice=0)
        time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join(5)
        lines = (self.tmp / "ATM2.log").read_text().splitlines()
        self.assertGreater(len(lines), 1)
        self.assertIn("ARRESTO", lines[-1])
        self.assertEqual(sum(1 for ln in lines if " ARRESTO " in ln), 1)
        for ln in lines:
            self.assertEqual(S.check_line("ATM2", 0, ln)[1], [])

    def test_stream_errors_ignored(self):
        """P1-20: errori sullo stream ignorati, la riga va comunque nel file."""
        logger = self.make(stream=_BrokenStream())
        line = logger.log("TOKEN_CREATO", seq=0, saldo=1000)
        logger.close()
        self.assertTrue(line)
        self.assertEqual((self.tmp / "ATM2.log").read_text(), line + "\n")

    def test_unknown_event_rejected(self):
        """P1-20: l'elenco degli eventi e' chiuso."""
        logger = self.make()
        with self.assertRaises(ValueError):
            logger.log("EVENTO_INVENTATO", seq=1)

    def test_unwritable_path_raises_oserror(self):
        """P1-I8: il costruttore propaga OSError se il file non si puo' creare."""
        blocker = self.tmp / "file_normale"
        blocker.write_text("x")
        with self.assertRaises(OSError):
            NodeLogger("ATM1", blocker / "sub" / "ATM1.log", False, stream=io.StringIO(),
                       clock=self.clock)


if __name__ == "__main__":
    unittest.main()
