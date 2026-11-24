"""Test d'integrazione di un solo nodo reale con predecessore e successore finti.

Il test fa da predecessore (si connette alla porta del nodo e invia righe) e da successore
(ascolta sulla porta del successore e legge cio' che il nodo inoltra). I byte attesi sono
scritti alla lettera nel formato del protocollo (JSON compatto su una riga), non calcolati con il codice di produzione.
"""
from __future__ import annotations

import re
import socket
import subprocess
import time
import unittest

import support as S

TOKEN_1_FOR_ATM2 = b'{"type":"TOKEN","seq":1,"balance":1000,"sender":"ATM1","receiver":"ATM2"}\n'
TOKEN_5_FOR_ATM2 = b'{"type":"TOKEN","seq":5,"balance":400,"sender":"ATM1","receiver":"ATM2"}\n'
TOKEN_1_FOR_ATM3 = b'{"type":"TOKEN","seq":1,"balance":1000,"sender":"ATM1","receiver":"ATM3"}\n'
FWD_2_800 = b'{"type":"TOKEN","seq":2,"balance":800,"sender":"ATM2","receiver":"ATM3"}\n'
FWD_2_1000 = b'{"type":"TOKEN","seq":2,"balance":1000,"sender":"ATM2","receiver":"ATM3"}\n'
FWD_6_400 = b'{"type":"TOKEN","seq":6,"balance":400,"sender":"ATM2","receiver":"ATM3"}\n'
WAIT = 10.0


def recv_line(conn: socket.socket, timeout: float = WAIT) -> bytes:
    """Legge fino al primo '\\n' (incluso) o fino a timeout/EOF."""
    conn.settimeout(0.2)
    data = b""
    end = time.monotonic() + timeout
    while b"\n" not in data and time.monotonic() < end:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def recv_nothing(conn: socket.socket, wait: float) -> bytes:
    """Byte ricevuti in ``wait`` secondi (atteso: nessuno, o EOF)."""
    conn.settimeout(wait)
    try:
        return conn.recv(4096)
    except socket.timeout:
        return b""
    except OSError:
        return b""


class SingleNodeCase(unittest.TestCase):
    """ATM2 reale; il test fa da ATM1 (predecessore) e da ATM3 (successore)."""

    def setUp(self):
        S.require_code()
        self.h = S.Harness()
        self.addCleanup(self.h.close)
        _, ports = S.allocate_block()
        self.node_port, self.succ_port = ports["ATM2"], ports["ATM3"]

    # -- utilita' -------------------------------------------------------------------------
    def fail_with_logs(self, msg: str) -> None:
        self.fail(f"{msg}\n{self.h.dump()}")

    def listen_successor(self) -> socket.socket:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.h.add_closer(srv.close)
        srv.bind(("127.0.0.1", self.succ_port))
        srv.listen(1)
        return srv

    def start(self, *, scenario=None, stdin=subprocess.DEVNULL, with_successor=True,
              wait_connected=True):
        srv = self.listen_successor() if with_successor else None
        self.np = self.h.start_node("ATM2", self.node_port, "ATM3", self.succ_port,
                                    scenario=scenario, stdin=stdin)
        if not self.h.wait_listening("ATM2"):
            self.fail_with_logs("ATM2 non e' in ascolto entro 5 s")
        self.succ = None
        if srv is not None and wait_connected:
            if self.wait("CONNESSIONE_OK") is None:
                self.fail_with_logs("ATM2 non si connette al successore finto")
            srv.settimeout(WAIT)
            self.succ, _ = srv.accept()
            self.h.add_closer(self.succ.close)
        return srv

    def connect_predecessor(self) -> socket.socket:
        before = self.count("PREDECESSORE_CONNESSO")
        pred = socket.create_connection(("127.0.0.1", self.node_port), timeout=5)
        self.h.add_closer(pred.close)
        if not S.wait_until(lambda: self.count("PREDECESSORE_CONNESSO") > before, WAIT):
            self.fail_with_logs("PREDECESSORE_CONNESSO non registrato")
        return pred

    def wait(self, event: str, fields_regex: str = "", timeout: float = WAIT):
        return S.wait_log(self.np.log_path, rf"\b{event}\b +{fields_regex}", timeout)

    def count(self, event: str) -> int:
        return sum(1 for ln in self.h.lines("ATM2") if re.search(rf"\] .{{3}} {event} ", ln))

    def subseq(self) -> list[str]:
        return S.token_subsequence_strings(self.h.lines("ATM2"))

    def assert_format(self, self_terminated: bool):
        errs = S.check_format("ATM2", self.h.lines("ATM2"), self_terminated, self.h.raw("ATM2"))
        if errs:
            self.fail_with_logs("formato del log:\n" + S.format_errors(errs))

    def wait_exit(self, timeout: float = WAIT):
        try:
            return self.np.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.fail_with_logs(f"il nodo non termina entro {timeout} s")


class TokenForwardingTest(SingleNodeCase):
    def test_valid_token_forwarded_exactly(self):
        """P1-04, P1-12, P1-I1, P1-09: ricevuto il token seq=1 saldo=1000, ATM2 esegue
        PRELIEVO 200 (5 passi nei log) e inoltra al successore esattamente
        {"type":"TOKEN","seq":2,"balance":800,"sender":"ATM2","receiver":"ATM3"}\\n."""
        self.start(scenario="scenari/riferimento/ATM2.txt")
        pred = self.connect_predecessor()
        pred.sendall(TOKEN_1_FOR_ATM2)
        got = recv_line(self.succ)
        self.assertEqual(got, FWD_2_800, "byte inoltrati al successore\n" + self.h.dump())
        self.assertTrue(S.wait_until(lambda: "TOKEN_INOLTRATO seq=2 saldo=800 a=ATM3"
                                     in self.subseq(), 5))
        self.assertEqual(self.subseq(), [
            "TOKEN_RICEVUTO seq=1 saldo=1000 da=ATM1",
            "INIZIO_TRANSAZIONE seq=1 saldo=1000 op=PRELIEVO importo=200",
            "SALDO_AGGIORNATO seq=1 saldo=800 prima=1000 variazione=-200",
            "FINE_TRANSAZIONE seq=1 saldo=800 esito=ACCETTATA",
            "TOKEN_INOLTRATO seq=2 saldo=800 a=ATM3"], self.h.dump())

    def test_second_possession_without_requests(self):
        """P1-15, P1-17, P1-I5: al possesso successivo, con la coda vuota, ATM2 registra
        NESSUNA_TRANSAZIONE e inoltra seq=6 saldo=400; resta attivo in attesa del token."""
        self.start(scenario="scenari/riferimento/ATM2.txt")
        pred = self.connect_predecessor()
        pred.sendall(TOKEN_1_FOR_ATM2)
        self.assertEqual(recv_line(self.succ), FWD_2_800, self.h.dump())
        pred.sendall(TOKEN_5_FOR_ATM2)
        self.assertEqual(recv_line(self.succ), FWD_6_400, self.h.dump())
        self.assertTrue(S.wait_until(lambda: len(self.subseq()) >= 8, 5), self.h.dump())
        self.assertEqual(self.subseq()[5:], ["TOKEN_RICEVUTO seq=5 saldo=400 da=ATM1",
                                             "NESSUNA_TRANSAZIONE seq=5 saldo=400",
                                             "TOKEN_INOLTRATO seq=6 saldo=400 a=ATM3"])
        self.assertIsNone(self.np.proc.poll(), "il nodo non deve terminare (--giri 0)")
        self.assert_format(self_terminated=False)

    def test_only_token_messages_are_exchanged(self):
        """P1-04, P1-05: l'unico messaggio e' TOKEN; nessun ack o heartbeat verso il
        successore, nulla verso il predecessore."""
        self.start()
        pred = self.connect_predecessor()
        self.assertEqual(recv_nothing(self.succ, 0.5), b"", "byte inattesi prima del token")
        pred.sendall(TOKEN_1_FOR_ATM2)
        self.assertEqual(recv_line(self.succ), FWD_2_1000, self.h.dump())
        self.assertEqual(recv_nothing(self.succ, 1.0), b"", "byte inattesi dopo il token")
        self.assertEqual(recv_nothing(pred, 0.3), b"", "il nodo ha scritto al predecessore")

    def test_startup_log_lines(self):
        """P1-09, P1-I8: righe di avvio con successore e porte esatte della CLI."""
        self.start(scenario="scenari/riferimento/ATM2.txt")
        lines = self.h.lines("ATM2")
        expected = [
            ("NODO_AVVIATO", f"successore=ATM3 porta_successore={self.succ_port} ritardo=0.1"),
            ("SCENARIO_CARICATO", "transazioni=1"),
            ("IN_ASCOLTO", f"porta={self.node_port}"),
        ]
        entries, _ = S.parse_log("ATM2", lines)
        self.assertEqual([(e.event, e.fields_text) for e in entries[:3]], expected, self.h.dump())
        ok = [e for e in entries if e.event == "CONNESSIONE_OK"]
        self.assertTrue(ok and re.fullmatch(
            rf"successore=ATM3 porta_successore={self.succ_port} tentativi=[1-9]\d*",
            ok[0].fields_text), self.h.dump())
        self.assert_format(self_terminated=False)

    def test_token_kept_until_successor_listens(self):
        """P1-I3: un token che arriva mentre il successore non e' pronto resta
        al nodo e viene inoltrato appena la connessione riesce; CONNESSIONE_OK precede
        TOKEN_RICEVUTO."""
        self.start(with_successor=False)
        if self.wait("CONNESSIONE_ATTESA") is None:
            self.fail_with_logs("manca CONNESSIONE_ATTESA con il successore assente")
        pred = self.connect_predecessor()
        pred.sendall(TOKEN_1_FOR_ATM2)
        time.sleep(1.0)
        self.assertNotIn("TOKEN_INOLTRATO", " ".join(self.subseq()), self.h.dump())
        srv = self.listen_successor()
        srv.settimeout(WAIT)
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            self.fail_with_logs("il nodo non si connette al successore comparso")
        self.h.add_closer(conn.close)
        self.assertEqual(recv_line(conn), FWD_2_1000, self.h.dump())
        events = [e.event for e in S.parse_log("ATM2", self.h.lines("ATM2"))[0]]
        self.assertLess(events.index("CONNESSIONE_OK"), events.index("TOKEN_RICEVUTO"),
                        self.h.dump())


class MalformedMessagesTest(SingleNodeCase):
    def test_malformed_line_discarded_connection_kept(self):
        """P1-04: una riga non JSON produce ERRORE tipo=messaggio_malformato, viene
        scartata, il nodo resta attivo e la stessa connessione resta utilizzabile."""
        self.start()
        pred = self.connect_predecessor()
        pred.sendall(b"questo non e' json\n")
        if self.wait("ERRORE", "tipo=messaggio_malformato") is None:
            self.fail_with_logs("manca ERRORE tipo=messaggio_malformato")
        self.assertIsNone(self.np.proc.poll(), "il nodo non deve terminare")
        pred.sendall(TOKEN_1_FOR_ATM2)
        self.assertEqual(recv_line(self.succ), FWD_2_1000, self.h.dump())
        self.assertEqual(self.count("PREDECESSORE_CHIUSO"), 0, "connessione chiusa dal nodo\n"
                         + self.h.dump())
        self.assertEqual(self.subseq(), ["TOKEN_RICEVUTO seq=1 saldo=1000 da=ATM1",
                                         "NESSUNA_TRANSAZIONE seq=1 saldo=1000",
                                         "TOKEN_INOLTRATO seq=2 saldo=1000 a=ATM3"])
        self.assert_format(self_terminated=False)

    def test_wrong_fields_discarded(self):
        """P1-04, P1-14: token con chiave in piu', saldo decimale o tipo diverso sono
        scartati con ERRORE e non producono alcun possesso."""
        self.start()
        pred = self.connect_predecessor()
        bad = [b'{"type":"TOKEN","seq":1,"balance":1000,"sender":"ATM1","receiver":"ATM2","x":1}\n',
               b'{"type":"TOKEN","seq":1,"balance":1000.0,"sender":"ATM1","receiver":"ATM2"}\n',
               b'{"type":"ACK","seq":1,"balance":1000,"sender":"ATM1","receiver":"ATM2"}\n']
        for line in bad:
            pred.sendall(line)
        if not S.wait_until(lambda: self.count("ERRORE") >= 3, WAIT):
            self.fail_with_logs("attesi 3 ERRORE tipo=messaggio_malformato")
        self.assertEqual(self.subseq(), [], self.h.dump())
        self.assertEqual(recv_nothing(self.succ, 0.5), b"")
        self.assertIsNone(self.np.proc.poll())

    def test_oversized_line_closes_connection(self):
        """P1-04: riga oltre 4096 byte senza '\\n' -> ERRORE messaggio_malformato,
        connessione chiusa (PREDECESSORE_CHIUSO); un nuovo predecessore viene accettato."""
        self.start()
        pred = self.connect_predecessor()
        try:
            pred.sendall(b"a" * 5000)
        except OSError:
            pass
        if self.wait("ERRORE", "tipo=messaggio_malformato") is None \
                or self.wait("PREDECESSORE_CHIUSO") is None:
            self.fail_with_logs("attesi ERRORE messaggio_malformato e PREDECESSORE_CHIUSO")
        self.assertIsNone(self.np.proc.poll())
        pred2 = self.connect_predecessor()
        pred2.sendall(TOKEN_1_FOR_ATM2)
        self.assertEqual(recv_line(self.succ), FWD_2_1000, self.h.dump())

    def test_partial_message_at_close_is_not_decoded(self):
        """P1-14: un token completo ma senza '\\n' alla chiusura della connessione
        NON viene decodificato: ERRORE messaggio_malformato seguito da PREDECESSORE_CHIUSO,
        nessun TOKEN_RICEVUTO, nulla inoltrato. Dopo la riconnessione il token vero passa una
        sola volta."""
        self.start()
        pred = self.connect_predecessor()
        pred.sendall(TOKEN_1_FOR_ATM2.rstrip(b"\n"))
        time.sleep(0.3)
        pred.close()
        if self.wait("PREDECESSORE_CHIUSO") is None:
            self.fail_with_logs("manca PREDECESSORE_CHIUSO")
        entries, _ = S.parse_log("ATM2", self.h.lines("ATM2"))
        events = [e.event for e in entries]
        err_idx = [i for i, e in enumerate(entries)
                   if e.event == "ERRORE" and e.fields.get("tipo") == "messaggio_malformato"]
        self.assertTrue(err_idx, "manca ERRORE messaggio_malformato\n" + self.h.dump())
        self.assertLess(err_idx[0], events.index("PREDECESSORE_CHIUSO"), self.h.dump())
        self.assertNotIn("TOKEN_RICEVUTO", events, self.h.dump())
        self.assertEqual(recv_nothing(self.succ, 0.5), b"", "frammento inoltrato come token")
        pred2 = self.connect_predecessor()
        pred2.sendall(TOKEN_1_FOR_ATM2)
        self.assertEqual(recv_line(self.succ), FWD_2_1000, self.h.dump())
        self.assertEqual(recv_nothing(self.succ, 0.5), b"", "token duplicato")
        received = [s for s in self.subseq() if s.startswith("TOKEN_RICEVUTO")]
        self.assertEqual(received, ["TOKEN_RICEVUTO seq=1 saldo=1000 da=ATM1"], self.h.dump())

    def test_wrong_receiver_exits_with_code_5(self):
        """P1-14: token destinato ad ATM3 ricevuto da ATM2 -> ERRORE
        tipo=destinatario_errato, ARRESTO motivo=errore codice=5 come ultima riga, codice
        d'uscita 5, nessun TOKEN_RICEVUTO ne' TOKEN_PERSO, nulla inoltrato."""
        self.start()
        pred = self.connect_predecessor()
        pred.sendall(TOKEN_1_FOR_ATM3)
        code = self.wait_exit()
        self.assertEqual(code, 5, self.h.dump())
        lines = self.h.lines("ATM2")
        entries, _ = S.parse_log("ATM2", lines)
        events = [e.event for e in entries]
        self.assertIn("destinatario_errato",
                      [e.fields.get("tipo") for e in entries if e.event == "ERRORE"], self.h.dump())
        self.assertTrue(lines and lines[-1].endswith("ARRESTO               motivo=errore codice=5"),
                        self.h.dump())
        self.assertNotIn("TOKEN_RICEVUTO", events)
        self.assertNotIn("TOKEN_PERSO", events)
        self.assertEqual(recv_nothing(self.succ, 0.5), b"", "token inoltrato")
        self.assert_format(self_terminated=True)


class KeyboardCommandsTest(SingleNodeCase):
    def test_commands_status_help_quit(self):
        """P1-I7: comandi da stdin rediretto: deposito accodato (in_coda=1), errori
        classificati con testo, riga vuota ignorata, stato, aiuto, esci -> ARRESTO
        motivo=comando_esci codice=0 come ultima riga e codice d'uscita 0."""
        self.start(stdin=subprocess.PIPE)
        commands = b"d 50\nversa 3\n\n  P  # solo\nstato extra\nd -5\nstato\naiuto\nesci\n"
        self.np.proc.stdin.write(commands)
        self.np.proc.stdin.flush()
        self.np.proc.stdin.close()
        code = self.wait_exit()
        self.assertEqual(code, 0, self.h.dump())
        keyboard_events = {"COMANDO_ACCETTATO", "COMANDO_NON_VALIDO", "STATO", "AIUTO", "ARRESTO"}
        entries, _ = S.parse_log("ATM2", self.h.lines("ATM2"))
        got = [e.short for e in entries if e.event in keyboard_events]
        self.assertEqual(got, [
            "COMANDO_ACCETTATO op=DEPOSITO importo=50 in_coda=1",
            'COMANDO_NON_VALIDO motivo=comando_sconosciuto testo="versa 3"',
            'COMANDO_NON_VALIDO motivo=importo_mancante testo="P  # solo"',
            'COMANDO_NON_VALIDO motivo=troppi_argomenti testo="stato extra"',
            'COMANDO_NON_VALIDO motivo=importo_non_valido testo="d -5"',
            "STATO token=no in_coda=1 ultimo_seq=- saldo_visto=-",
            'AIUTO uso="deposito N | prelievo N | stato | aiuto | esci"',
            "ARRESTO motivo=comando_esci codice=0",
        ], self.h.dump())
        self.assertNotIn("TOKEN_PERSO", [e.event for e in entries])
        self.assert_format(self_terminated=True)

    def test_quit_while_connecting(self):
        """P1-I7: 'esci' mentre il successore non risponde -> ARRESTO
        motivo=comando_esci codice=0, codice d'uscita 0, nessun TOKEN_PERSO (nessun token)."""
        self.start(stdin=subprocess.PIPE, with_successor=False)
        if self.wait("CONNESSIONE_ATTESA") is None:
            self.fail_with_logs("manca CONNESSIONE_ATTESA")
        self.np.proc.stdin.write(b"esci\n")
        self.np.proc.stdin.flush()
        self.np.proc.stdin.close()
        code = self.wait_exit()
        self.assertEqual(code, 0, self.h.dump())
        lines = self.h.lines("ATM2")
        self.assertTrue(lines[-1].endswith("ARRESTO               motivo=comando_esci codice=0"),
                        self.h.dump())
        events = [e.event for e in S.parse_log("ATM2", lines)[0]]
        self.assertNotIn("CONNESSIONE_OK", events)
        self.assertNotIn("TOKEN_PERSO", events)
        self.assert_format(self_terminated=True)


if __name__ == "__main__":
    unittest.main()
