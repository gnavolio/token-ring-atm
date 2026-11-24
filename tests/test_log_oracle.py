"""Auto-verifica dell'oracolo dei test (regex dei log, invarianti sui log, modello di riferimento).

Questi test NON usano il codice di produzione: applicano il verificatore dei log di
``support.py`` ai log di riferimento e alle righe d'esempio (che devono passare) e a log mutati
(che devono essere rifiutati). Servono a dimostrare che un verde dei test d'integrazione
significa davvero "invarianti rispettati".
"""
from __future__ import annotations

import unittest
from collections import Counter

import support as S
from golden_logs import EXAMPLE_LINES, GOLDEN_DEMO, GOLDEN_RIFIUTO, TOKENSEQ_RIFERIMENTO

REFERENCE_QUEUES = {"ATM2": [("PRELIEVO", 200)], "ATM3": [("DEPOSITO", 100)],
                    "ATM4": [("PRELIEVO", 500)]}
REFUSAL_QUEUES = {"ATM2": [("PRELIEVO", 1500)], "ATM3": [("DEPOSITO", 100)],
                  "ATM4": [("PRELIEVO", 1100)]}
ALL_TERMINATED = {n: True for n in S.RING}


def _replace(lines, old_prefix_event, new_line=None, *, insert_before=None, insert_after=None):
    out = []
    for ln in lines:
        if insert_before and insert_before in ln:
            out.append(new_line)
        out.append(ln)
        if insert_after and insert_after in ln:
            out.append(new_line)
    return out


class OfficialRegexOnDesignExamplesTest(unittest.TestCase):
    """Le regex ufficiali accettano tutte le righe d'esempio di riferimento."""

    def test_every_design_example_line_matches(self):
        """P1-20, P1-I8: ogni riga d'esempio e dei log di riferimento soddisfa LINE_RE,
        FIELDS_RE, EVENT_MARK e le colonne fisse del formato."""
        lines = list(EXAMPLE_LINES)
        for d in (GOLDEN_DEMO, GOLDEN_RIFIUTO):
            for v in d.values():
                lines.extend(v)
        errors = []
        for i, ln in enumerate(lines):
            _, errs = S.check_line(None, i, ln)
            errors.extend(errs)
        self.assertEqual(errors, [], "righe del design rifiutate dall'oracolo:\n"
                         + S.format_errors(errors))

    def test_examples_cover_all_24_events(self):
        """P1-20: le righe d'esempio coprono tutti i 24 eventi (almeno 2 per evento)."""
        counts = Counter()
        for ln in EXAMPLE_LINES:
            e, _ = S.check_line(None, 0, ln)
            if e is not None:
                counts[e.event] += 1
        self.assertEqual(set(counts), set(S.EVENT_MARK))
        self.assertTrue(all(c >= 2 for c in counts.values()), counts)

    def test_wrong_mark_is_rejected(self):
        """P1-20: una marca sbagliata (vecchia ' | ') o fuori colonna viene rilevata."""
        good = "11:00:01.097 [ATM2] !!! TRANSAZIONE_RIFIUTATA seq=1 saldo=1000 motivo=saldo_insufficiente"
        self.assertEqual(S.check_line("ATM2", 0, good)[1], [])
        for bad in (good.replace("!!!", " | "),
                    good.replace("[ATM2] !!! ", "[ATM2]  !!!"),
                    good + " ",
                    good.replace("saldo=1000", "saldo=01000")):
            with self.subTest(bad=bad):
                self.assertNotEqual(S.check_line("ATM2", 0, bad)[1], [])

    def test_wrong_node_prefix_is_rejected(self):
        """P1-I6: il prefisso [ATMn] deve essere quello del nodo del file."""
        line = GOLDEN_RIFIUTO["ATM1"][0]
        self.assertNotEqual(S.check_line("ATM2", 0, line)[1], [])


class ReferenceModelTest(unittest.TestCase):
    """Il modello di riferimento riproduce le sottosequenze di riferimento."""

    def test_model_matches_tokenseq_golden(self):
        """P1-19: il modello riproduce la sottosequenza attesa dello scenario di riferimento (--giri 1)."""
        self.assertEqual(S.model_token_subsequences(REFERENCE_QUEUES, 1), TOKENSEQ_RIFERIMENTO)

    def test_model_matches_refusal_golden(self):
        """P1-I2: il modello riproduce la sottosequenza dei log attesi dello scenario rifiuto."""
        expected = {n: S.token_subsequence_strings(GOLDEN_RIFIUTO[n]) for n in S.RING}
        self.assertEqual(S.model_token_subsequences(REFUSAL_QUEUES, 1), expected)

    def test_model_matches_demo_golden_prefix(self):
        """P1-15, P1-19: il primo giro dei log attesi della demo coincide con il modello."""
        model = S.model_token_subsequences(REFERENCE_QUEUES, 2)
        for n in S.RING:
            demo = S.token_subsequence_strings(GOLDEN_DEMO[n])
            self.assertEqual(model[n][:len(demo)], demo, n)


class InvariantsOnGoldenTest(unittest.TestCase):
    """Gli invarianti accettano i log di riferimento."""

    def test_refusal_golden_satisfies_all_invariants(self):
        """P1-08, P1-14, P1-20: i log attesi completi dello scenario rifiuto soddisfano tutti gli invarianti."""
        errors = S.check_all(GOLDEN_RIFIUTO, ALL_TERMINATED)
        self.assertEqual(errors, [], S.format_errors(errors))

    def test_refusal_golden_balances(self):
        """P1-I2: saldi inoltrati 1000, 1000, 1100, 0."""
        self.assertEqual(S.forwarded_balances(GOLDEN_RIFIUTO), {1: 1000, 2: 1000, 3: 1100, 4: 0})

    def test_demo_golden_possessions_with_open_last_possession(self):
        """P1-I5: i log attesi della demo, estratti di un'esecuzione infinita, sono validi con
        l'ultimo possesso aperto (processi non terminati da se')."""
        for n in S.RING:
            entries, errs = S.parse_log(n, GOLDEN_DEMO[n])
            self.assertEqual(errs, [])
            self.assertEqual(S.check_possessions(n, entries, self_terminated=False), [], n)


class InvariantMutationsTest(unittest.TestCase):
    """Log mutati: l'oracolo DEVE rilevare ogni violazione."""

    def check_all(self, logs, terminated=None):
        return S.check_all(logs, terminated or ALL_TERMINATED)

    def mutated(self, node, lines):
        logs = {n: list(v) for n, v in GOLDEN_RIFIUTO.items()}
        logs[node] = lines
        return logs

    def assertDetected(self, errors, needle=None):
        self.assertNotEqual(errors, [], "violazione NON rilevata dall'oracolo")
        if needle is not None:
            self.assertTrue(any(needle in e for e in errors), S.format_errors(errors))

    def test_two_transactions_in_one_possession(self):
        """P1-I5: due transazioni nello stesso possesso -> violazione rilevata."""
        g = GOLDEN_RIFIUTO["ATM3"]
        extra = [S.make_line("11:00:01.202", "ATM3", "INIZIO_TRANSAZIONE",
                             "seq=2 saldo=1100 op=DEPOSITO importo=5"),
                 S.make_line("11:00:01.202", "ATM3", "SALDO_AGGIORNATO",
                             "seq=2 saldo=1105 prima=1100 variazione=+5"),
                 S.make_line("11:00:01.202", "ATM3", "FINE_TRANSAZIONE",
                             "seq=2 saldo=1105 esito=ACCETTATA")]
        idx = next(i for i, ln in enumerate(g) if "FINE_TRANSAZIONE" in ln) + 1
        self.assertDetected(self.check_all(self.mutated("ATM3", g[:idx] + extra + g[idx:])),
                            "corpo del possesso")

    def test_body_before_token_retired(self):
        """P1-I5: TOKEN_RITIRATO deve avere corpo vuoto."""
        g = GOLDEN_RIFIUTO["ATM1"]
        line = S.make_line("11:00:01.700", "ATM1", "NESSUNA_TRANSAZIONE", "seq=4 saldo=0")
        self.assertDetected(self.check_all(self.mutated(
            "ATM1", _replace(g, None, line, insert_before="TOKEN_RITIRATO"))), "TOKEN_RITIRATO")

    def test_line_after_arresto_on_complete_file(self):
        """P1-20: una riga qualsiasi dopo ARRESTO nel file completo e' rilevata, anche se
        non appartiene alla sottosequenza del token."""
        g = GOLDEN_RIFIUTO["ATM3"] + [S.make_line("11:00:01.304", "ATM3", "PREDECESSORE_CHIUSO",
                                                  "remoto=127.0.0.1:52002")]
        self.assertDetected(self.check_all(self.mutated("ATM3", g)), "ultima riga")

    def test_overlapping_critical_sections(self):
        """P1-08, P1-11: sezioni critiche sovrapposte tra nodi."""
        g = [ln.replace("11:00:01.200 [ATM3] >>>", "11:00:01.050 [ATM3] >>>")
             for ln in GOLDEN_RIFIUTO["ATM3"]]
        self.assertDetected(self.check_all(self.mutated("ATM3", g)), "inv.8")

    def test_duplicate_token(self):
        """P1-14: lo stesso seq ricevuto due volte -> due token."""
        logs = {n: list(v) for n, v in GOLDEN_RIFIUTO.items()}
        logs["ATM4"] = [S.make_line("11:00:00.500", "ATM4", "TOKEN_RICEVUTO",
                                    "seq=2 saldo=1000 da=ATM2"),
                        S.make_line("11:00:00.500", "ATM4", "NESSUNA_TRANSAZIONE",
                                    "seq=2 saldo=1000"),
                        S.make_line("11:00:00.600", "ATM4", "TOKEN_INOLTRATO",
                                    "seq=3 saldo=1000 a=ATM1")] + logs["ATM4"]
        self.assertDetected(self.check_all(logs), "inv.3a")

    def test_balance_changed_in_transit(self):
        """P1-I1: il saldo ricevuto deve essere quello inoltrato."""
        g = [ln.replace("TOKEN_RICEVUTO        seq=2 saldo=1000", "TOKEN_RICEVUTO        seq=2 saldo=999")
             for ln in GOLDEN_RIFIUTO["ATM3"]]
        self.assertDetected(self.check_all(self.mutated("ATM3", g)), "inv.3c")

    def test_two_creators(self):
        """P1-14, P1-18: due TOKEN_CREATO."""
        g = [S.make_line("11:00:00.100", "ATM2", "TOKEN_CREATO", "seq=0 saldo=1000")] \
            + GOLDEN_RIFIUTO["ATM2"]
        self.assertDetected(self.check_all(self.mutated("ATM2", g)), "inv.2")

    def test_creator_without_retire(self):
        """P1-20: creatore terminato da se' senza TOKEN_RITIRATO (possesso non chiuso)."""
        g = [ln for ln in GOLDEN_RIFIUTO["ATM1"] if "TOKEN_RITIRATO" not in ln]
        self.assertDetected(self.check_all(self.mutated("ATM1", g)))

    def test_wrong_successor(self):
        """P1-09: un inoltro verso un nodo che non e' il successore."""
        logs = {n: list(v) for n, v in GOLDEN_RIFIUTO.items()}
        logs["ATM1"] = [ln.replace("a=ATM2", "a=ATM3") for ln in logs["ATM1"]]
        self.assertDetected(self.check_all(logs))

    def test_token_lost_not_followed_by_arresto(self):
        """P1-20: TOKEN_PERSO deve essere seguito da ARRESTO."""
        lines = [S.make_line("10:00:00.000", "ATM2", "TOKEN_RICEVUTO", "seq=1 saldo=1000 da=ATM1"),
                 S.make_line("10:00:00.001", "ATM2", "TOKEN_PERSO", "seq=1 saldo=1000"),
                 S.make_line("10:00:00.002", "ATM2", "NESSUNA_TRANSAZIONE", "seq=1 saldo=1000")]
        entries, _ = S.parse_log("ATM2", lines)
        self.assertNotEqual(S.check_possessions("ATM2", entries, self_terminated=False), [])

    def test_valid_fault_cases_are_accepted(self):
        """P1-20: casi di guasto validi (Ctrl+C in sezione critica, token in inbox, reinvio
        corretto, invio fallito seguito da TOKEN_PERSO) NON sono violazioni."""
        L = S.make_line
        cases = {
            "ctrl_c_in_sezione_critica": [
                L("10:00:00.000", "ATM2", "TOKEN_RICEVUTO", "seq=1 saldo=1000 da=ATM1"),
                L("10:00:00.001", "ATM2", "INIZIO_TRANSAZIONE", "seq=1 saldo=1000 op=PRELIEVO importo=200"),
                L("10:00:00.002", "ATM2", "SALDO_AGGIORNATO", "seq=1 saldo=800 prima=1000 variazione=-200"),
                L("10:00:00.003", "ATM2", "TOKEN_PERSO", "seq=1 saldo=800"),
                L("10:00:00.004", "ATM2", "ARRESTO", "motivo=interruzione codice=0")],
            "token_in_inbox": [
                L("10:00:00.000", "ATM2", "TOKEN_PERSO", "seq=5 saldo=400"),
                L("10:00:00.001", "ATM2", "ARRESTO", "motivo=comando_esci codice=0")],
            "reinvio_corretto": [
                L("10:00:00.000", "ATM2", "TOKEN_RICEVUTO", "seq=1 saldo=1000 da=ATM1"),
                L("10:00:00.001", "ATM2", "NESSUNA_TRANSAZIONE", "seq=1 saldo=1000"),
                L("10:00:00.100", "ATM2", "TOKEN_INOLTRATO", "seq=2 saldo=1000 a=ATM3"),
                L("10:00:00.101", "ATM2", "SUCCESSORE_PERSO", "successore=ATM3 porta_successore=5003"),
                L("10:00:01.101", "ATM2", "CONNESSIONE_OK", "successore=ATM3 porta_successore=5003 tentativi=1"),
                L("10:00:01.102", "ATM2", "TOKEN_INOLTRATO", "seq=2 saldo=1000 a=ATM3")],
            "invio_fallito_poi_token_perso": [
                L("10:00:00.000", "ATM2", "TOKEN_RICEVUTO", "seq=1 saldo=1000 da=ATM1"),
                L("10:00:00.001", "ATM2", "NESSUNA_TRANSAZIONE", "seq=1 saldo=1000"),
                L("10:00:00.100", "ATM2", "TOKEN_INOLTRATO", "seq=2 saldo=1000 a=ATM3"),
                L("10:00:00.101", "ATM2", "SUCCESSORE_PERSO", "successore=ATM3 porta_successore=5003"),
                L("10:00:01.101", "ATM2", "CONNESSIONE_ATTESA", "successore=ATM3 porta_successore=5003 tentativo=1"),
                L("10:00:01.500", "ATM2", "TOKEN_PERSO", "seq=1 saldo=1000"),
                L("10:00:01.501", "ATM2", "ARRESTO", "motivo=comando_esci codice=0")],
        }
        for name, lines in cases.items():
            with self.subTest(name):
                entries, errs = S.parse_log("ATM2", lines)
                self.assertEqual(errs, [])
                self.assertEqual(S.check_possessions("ATM2", entries, self_terminated=True), [])
        entries, _ = S.parse_log("ATM2", cases["invio_fallito_poi_token_perso"])
        fwd = next(e for e in entries if e.event == "TOKEN_INOLTRATO")
        self.assertTrue(S._forward_failed(entries, fwd))

    def test_resend_without_successor_lost_is_rejected(self):
        """P1-14: stesso seq inoltrato due volte senza SUCCESSORE_PERSO."""
        L = S.make_line
        lines = [L("10:00:00.000", "ATM2", "TOKEN_RICEVUTO", "seq=1 saldo=1000 da=ATM1"),
                 L("10:00:00.001", "ATM2", "NESSUNA_TRANSAZIONE", "seq=1 saldo=1000"),
                 L("10:00:00.100", "ATM2", "TOKEN_INOLTRATO", "seq=2 saldo=1000 a=ATM3"),
                 L("10:00:01.102", "ATM2", "TOKEN_INOLTRATO", "seq=2 saldo=1000 a=ATM3")]
        entries, _ = S.parse_log("ATM2", lines)
        self.assertNotEqual(S.check_possessions("ATM2", entries, self_terminated=False), [])

    def test_wrong_variation_sign(self):
        """P1-12: variazione incoerente con l'operazione o con saldo - prima."""
        g = [ln.replace("saldo=1100 prima=1000 variazione=+100", "saldo=1100 prima=1000 variazione=+10")
             for ln in GOLDEN_RIFIUTO["ATM3"]]
        self.assertDetected(self.check_all(self.mutated("ATM3", g)))


if __name__ == "__main__":
    unittest.main()
