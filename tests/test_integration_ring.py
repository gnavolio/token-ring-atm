"""Test d'integrazione con 4 processi reali.

Ogni classe esegue UNA volta l'anello completo (in ``setUpClass``) e ne verifica le proprieta'
con metodi separati, uno per gruppo di requisiti. I nodi usano porte di test, log in una
cartella temporanea in %TEMP%, ``--ritardo 0.1 --giri N --no-colori``; vengono sempre
terminati (``addClassCleanup``) anche se un test fallisce.
"""
from __future__ import annotations

import subprocess
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import support as S
from golden_logs import GOLDEN_RIFIUTO, TOKENSEQ_RIFERIMENTO

REFERENCE = {n: f"scenari/riferimento/{n}.txt" for n in S.RING}
REFUSAL = {n: f"scenari/rifiuto/{n}.txt" for n in S.RING}
REFERENCE_QUEUES = {"ATM2": [("PRELIEVO", 200)], "ATM3": [("DEPOSITO", 100)],
                    "ATM4": [("PRELIEVO", 500)]}


@dataclass
class RingOutcome:
    problems: list[str]
    lines: dict[str, list[str]]
    raw: dict[str, str]
    returncodes: dict[str, int | None]
    self_terminated: dict[str, bool]
    dump: str
    extra: dict = field(default_factory=dict)


def start_ring_node(h: S.Harness, ports: Mapping[str, int], node: str, scenario, rounds: int,
                    stdin=subprocess.DEVNULL) -> S.NodeProcess:
    succ = S.SUCCESSOR[node]
    return h.start_node(node, ports[node], succ, ports[succ], scenario=scenario,
                        create_token=(node == "ATM1"), rounds=rounds, stdin=stdin)


def wait_started(h: S.Harness, node: str, problems: list[str]) -> None:
    if not h.wait_listening(node):
        problems.append(f"{node}: nessuna riga IN_ASCOLTO entro {S.LISTEN_TIMEOUT} s "
                        f"(returncode={h.nodes[node].proc.poll()})")


def collect(h: S.Harness, problems: list[str]) -> RingOutcome:
    alive = h.wait_all_exit(S.RUN_DEADLINE)
    if alive:
        problems.append(f"processi ancora attivi dopo {S.RUN_DEADLINE} s, terminati dal test: {alive}")
    return RingOutcome(
        problems=problems,
        lines={n: h.lines(n) for n in h.nodes},
        raw={n: h.raw(n) for n in h.nodes},
        returncodes={n: p.proc.returncode for n, p in h.nodes.items()},
        self_terminated={n: p.self_terminated for n, p in h.nodes.items()},
        dump=h.dump(),
    )


def run_ring(h: S.Harness, scenarios: Mapping[str, object], *, order: Sequence[str] = S.RING,
             gap: float = 0.0, rounds: int = 1) -> RingOutcome:
    """Ricetta del test d'integrazione: avvio nell'ordine dato (attendendo IN_ASCOLTO dopo ogni avvio,
    poi ``gap`` secondi), attesa della fine con scadenza complessiva di 30 s."""
    _, ports = S.allocate_block()
    problems: list[str] = []
    for node in order:
        start_ring_node(h, ports, node, scenarios.get(node), rounds)
        wait_started(h, node, problems)
        if gap:
            time.sleep(gap)
    return collect(h, problems)


class _RingCase(unittest.TestCase):
    """Base: esegue l'anello una volta per classe e offre le asserzioni comuni."""

    outcome: RingOutcome
    ROUNDS = 1

    @classmethod
    def run_scenario(cls, h: S.Harness) -> RingOutcome:  # pragma: no cover - astratto
        raise NotImplementedError

    @classmethod
    def expected_subsequences(cls) -> dict[str, list[str]]:  # pragma: no cover - astratto
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        S.require_code()
        h = S.Harness()
        cls.addClassCleanup(h.close)
        try:
            cls.outcome = cls.run_scenario(h)
        except BaseException:
            h.close()
            raise

    # -- utilita' -------------------------------------------------------------------------
    def fail_with_logs(self, title: str, errors: Sequence[str]) -> None:
        self.fail(f"{title}\n{S.format_errors(list(errors))}\n{self.outcome.dump}")

    def errors_with_prefix(self, *prefixes: str) -> list[str]:
        errs = S.check_all(self.outcome.lines, self.outcome.self_terminated,
                           raw_texts=self.outcome.raw)
        return [e for e in errs if e.startswith(prefixes)]


class RingChecksMixin:
    """Proprieta' verificate su ogni esecuzione dell'anello con ``--giri``."""

    def test_processes_exit_with_code_0(self):
        """P1-01, P1-03: 4 processi separati terminano da se' con codice 0 (--giri)."""
        o = self.outcome
        errors = list(o.problems)
        for n in S.RING:
            if o.returncodes.get(n) != 0 or not o.self_terminated.get(n):
                errors.append(f"{n}: returncode={o.returncodes.get(n)}, terminato da se'="
                              f"{o.self_terminated.get(n)}")
        if errors:
            self.fail_with_logs("processi non terminati correttamente", errors)

    def test_log_files_format_and_final_arresto(self):
        """P1-20, P1-I8: ogni riga di ogni file soddisfa le regex ufficiali con
        timestamp al millisecondo; l'ultima riga del file COMPLETO e' ARRESTO
        motivo=giri_completati codice=0 e compare una sola volta."""
        errors = []
        for n in S.RING:
            lines = self.outcome.lines.get(n, [])
            errors += S.check_format(n, lines, True, self.outcome.raw.get(n))
            if not lines or not lines[-1].endswith("ARRESTO               motivo=giri_completati codice=0"):
                errors.append(f"{n}: ultima riga {lines[-1] if lines else None!r}")
        if errors:
            self.fail_with_logs("formato dei log / ultima riga", errors)

    def test_token_subsequence_matches_expected(self):
        """P1-19, P1-20, P1-I3: la sottosequenza del token di ogni log e' esattamente quella attesa
        (log di riferimento in ``golden_logs.py`` o modello di ``support.py``), per qualunque ordine di avvio."""
        expected = self.expected_subsequences()
        errors = []
        for n in S.RING:
            got = S.token_subsequence_strings(self.outcome.lines.get(n, []))
            if got != expected[n]:
                errors.append(f"{n}:\n      atteso  {expected[n]}\n      ottenuto {got}")
        if errors:
            self.fail_with_logs("sottosequenza del token diversa dall'attesa", errors)

    def test_ring_order_and_successor(self):
        """P1-09, P1-19: l'ordine dei possessi e' ATM1, ATM2, ATM3, ATM4, ATM1, ...;
        ogni hop va dal nodo al suo successore."""
        errors = self.errors_with_prefix("inv.5", "inv.3b/P1-09")
        if errors:
            self.fail_with_logs("ordine dell'anello violato", errors)

    def test_single_token(self):
        """P1-14, P1-15: un solo creatore; ogni seq ricevuto una sola volta;
        inoltro e ricezione in corrispondenza biunivoca con saldo invariato; seq crescenti senza
        buchi, anche nella storia globale ordinata per tempo; ricezione dopo l'inoltro."""
        errors = self.errors_with_prefix("inv.2", "inv.3", "inv.4", "P1-14", "causalita'")
        if errors:
            self.fail_with_logs("unicita' del token violata", errors)

    def test_critical_sections_never_overlap(self):
        """P1-08, P1-11, P1-13, P1-20: gli intervalli [INIZIO, FINE] dei 4 nodi, presi
        dai timestamp dei file di log, non si sovrappongono mai e seguono l'ordine dei seq."""
        errors = self.errors_with_prefix("inv.8", "P1-08")
        if errors:
            self.fail_with_logs("sezioni critiche sovrapposte", errors)

    def test_possession_rules(self):
        """P1-08, P1-16, P1-17, P1-I5: ogni possesso contiene al massimo una
        transazione o NESSUNA_TRANSAZIONE; la sezione critica sta solo dentro un possesso;
        TOKEN_RITIRATO con corpo vuoto; saldi coerenti nella sezione critica."""
        errors = []
        for n in S.RING:
            entries, _ = S.parse_log(n, self.outcome.lines.get(n, []))
            errors += S.check_possessions(n, entries, self.outcome.self_terminated.get(n, False))
        if errors:
            self.fail_with_logs("regole del possesso violate", errors)

    def test_all_invariants(self):
        """P1-08, P1-14, P1-20: tutti gli invarianti sui log insieme (elenco in ``support.py``)."""
        errors = S.check_all(self.outcome.lines, self.outcome.self_terminated,
                             raw_texts=self.outcome.raw)
        if errors:
            self.fail_with_logs("invarianti sui log violati", errors)


# ===================================================================================
# Scenario di riferimento del testo d'esame in tre ordini di avvio (P1-19, P1-I3)
# ===================================================================================

class _ReferenceChecks(RingChecksMixin):
    @classmethod
    def expected_subsequences(cls):
        return {n: list(v) for n, v in TOKENSEQ_RIFERIMENTO.items()}

    def test_balances_1000_800_900_400(self):
        """P1-18, P1-19: saldi trasportati negli hop seq 1..4 = 1000, 800, 900, 400."""
        got = S.forwarded_balances(self.outcome.lines)
        if got != {1: 1000, 2: 800, 3: 900, 4: 400}:
            self.fail_with_logs("saldi del primo giro", [f"ottenuto {got}"])

    def test_node_without_requests_does_not_enter_critical_section(self):
        """P1-17: ATM1, senza richieste, registra NESSUNA_TRANSAZIONE e inoltra senza
        INIZIO_TRANSAZIONE."""
        lines = self.outcome.lines.get("ATM1", [])
        sub = S.token_subsequence_strings(lines)
        errors = []
        if any(s.startswith("INIZIO_TRANSAZIONE") for s in sub):
            errors.append("ATM1 entra in sezione critica")
        if "NESSUNA_TRANSAZIONE seq=0 saldo=1000" not in sub:
            errors.append("manca NESSUNA_TRANSAZIONE seq=0 saldo=1000")
        if errors:
            self.fail_with_logs("P1-17", errors)


class ReferenceScenarioNormalOrderTest(_ReferenceChecks, _RingCase):
    """Scenario di riferimento (saldo 1000; ATM2 -200, ATM3 +100, ATM4 -500), avvio ATM1, ATM2,
    ATM3, ATM4."""

    @classmethod
    def run_scenario(cls, h):
        return run_ring(h, REFERENCE, order=S.RING)


class ReferenceScenarioReverseOrderTest(_ReferenceChecks, _RingCase):
    """Scenario di riferimento, avvio in ordine inverso ATM4..ATM1 a circa 1 s l'uno dall'altro (P1-I3)."""

    @classmethod
    def run_scenario(cls, h):
        return run_ring(h, REFERENCE, order=tuple(reversed(S.RING)), gap=1.0)


class ReferenceScenarioMixedOrderTest(_ReferenceChecks, _RingCase):
    """Scenario di riferimento, avvio in ordine misto ATM3, ATM1, ATM4, ATM2 (P1-I3, "qualsiasi ordine")."""

    @classmethod
    def run_scenario(cls, h):
        return run_ring(h, REFERENCE, order=("ATM3", "ATM1", "ATM4", "ATM2"), gap=0.3)


# ===================================================================================
# Scenario con prelievo rifiutato (P1-I2)
# ===================================================================================

class RefusalScenarioTest(RingChecksMixin, _RingCase):
    """Scenario ``rifiuto``, avvio in ordine inverso a circa 0,3 s (come nei log attesi ``golden_logs.GOLDEN_RIFIUTO``)."""

    @classmethod
    def run_scenario(cls, h):
        return run_ring(h, REFUSAL, order=tuple(reversed(S.RING)), gap=0.3)

    @classmethod
    def expected_subsequences(cls):
        return {n: S.token_subsequence_strings(GOLDEN_RIFIUTO[n]) for n in S.RING}

    def test_withdrawal_over_balance_rejected(self):
        """P1-I2: in ATM2 TRANSAZIONE_RIFIUTATA motivo=saldo_insufficiente,
        FINE_TRANSAZIONE esito=RIFIUTATA con saldo = saldo letto = 1000, TOKEN_INOLTRATO seq=2
        saldo=1000; saldi inoltrati 1000, 1000, 1100, 0."""
        sub = S.token_subsequence_strings(self.outcome.lines.get("ATM2", []))
        errors = []
        for needed in ("INIZIO_TRANSAZIONE seq=1 saldo=1000 op=PRELIEVO importo=1500",
                       "TRANSAZIONE_RIFIUTATA seq=1 saldo=1000 motivo=saldo_insufficiente",
                       "FINE_TRANSAZIONE seq=1 saldo=1000 esito=RIFIUTATA",
                       "TOKEN_INOLTRATO seq=2 saldo=1000 a=ATM3"):
            if needed not in sub:
                errors.append(f"ATM2: manca {needed!r}")
        if any(s.startswith("SALDO_AGGIORNATO") for s in sub):
            errors.append("ATM2: SALDO_AGGIORNATO dopo un rifiuto")
        got = S.forwarded_balances(self.outcome.lines)
        if got != {1: 1000, 2: 1000, 3: 1100, 4: 0}:
            errors.append(f"saldi inoltrati {got}")
        if errors:
            self.fail_with_logs("prelievo oltre il saldo", errors)

    def test_full_balance_withdrawal_accepted(self):
        """P1-I2, P1-13: ATM4 preleva l'intero saldo (1100) e il saldo diventa 0."""
        sub = S.token_subsequence_strings(self.outcome.lines.get("ATM4", []))
        if "SALDO_AGGIORNATO seq=3 saldo=0 prima=1100 variazione=-1100" not in sub:
            self.fail_with_logs("prelievo dell'intero saldo", [f"ATM4: {sub}"])


# ===================================================================================
# Piu' giri: il token circola continuamente (P1-15)
# ===================================================================================

class MultipleRoundsTest(RingChecksMixin, _RingCase):
    """Scenario di riferimento con ``--giri 3``: dopo lo scenario il token continua a girare."""

    @classmethod
    def run_scenario(cls, h):
        return run_ring(h, REFERENCE, order=S.RING, rounds=3)

    @classmethod
    def expected_subsequences(cls):
        return S.model_token_subsequences(REFERENCE_QUEUES, 3)

    def test_token_keeps_circulating_after_scenario(self):
        """P1-15, P1-17: ATM1 riceve il token a seq 4, 8, 12; nei giri 2 e 3 tutti i nodi
        inoltrano con NESSUNA_TRANSAZIONE e il saldo resta 400."""
        errors = []
        received = [l for l in S.token_subsequence_strings(self.outcome.lines.get("ATM1", []))
                    if l.startswith("TOKEN_RICEVUTO")]
        if received != ["TOKEN_RICEVUTO seq=4 saldo=400 da=ATM4",
                        "TOKEN_RICEVUTO seq=8 saldo=400 da=ATM4",
                        "TOKEN_RICEVUTO seq=12 saldo=400 da=ATM4"]:
            errors.append(f"ATM1 ricezioni: {received}")
        balances = S.forwarded_balances(self.outcome.lines)
        expected = {k: (1000, 800, 900)[k - 1] if k <= 3 else 400 for k in range(1, 13)}
        if balances != expected:
            errors.append(f"saldi inoltrati {balances}")
        if errors:
            self.fail_with_logs("circolazione continua", errors)


# ===================================================================================
# Al massimo una transazione per possesso (P1-I5)
# ===================================================================================

class OneTransactionPerPossessionTest(RingChecksMixin, _RingCase):
    """ATM2 ha 2 richieste in coda: le esegue in 2 giri diversi (``--giri 2``)."""

    QUEUES = {"ATM2": [("PRELIEVO", 100), ("DEPOSITO", 30)]}

    @classmethod
    def run_scenario(cls, h):
        scen_dir = h.tmp / "scenari"
        scen_dir.mkdir()
        files = {}
        for n in S.RING:
            body = "# scenario di test (P1-I5)\n"
            if n == "ATM2":
                body += "PRELIEVO 100\nDEPOSITO 30\n"
            (scen_dir / f"{n}.txt").write_text(body, encoding="utf-8")
            files[n] = str(scen_dir / f"{n}.txt")
        return run_ring(h, files, order=S.RING, rounds=2)

    @classmethod
    def expected_subsequences(cls):
        return S.model_token_subsequences(cls.QUEUES, 2)

    def test_two_requests_in_two_different_rounds(self):
        """P1-I5, P1-17: ATM2 esegue PRELIEVO 100 al possesso seq=1 e DEPOSITO 30 al possesso
        seq=5; gli altri nodi non entrano mai in sezione critica."""
        errors = []
        starts = {}
        for n in S.RING:
            for s in S.token_subsequence_strings(self.outcome.lines.get(n, [])):
                if s.startswith("INIZIO_TRANSAZIONE"):
                    starts.setdefault(n, []).append(s)
        expected = {"ATM2": ["INIZIO_TRANSAZIONE seq=1 saldo=1000 op=PRELIEVO importo=100",
                             "INIZIO_TRANSAZIONE seq=5 saldo=900 op=DEPOSITO importo=30"]}
        if starts != expected:
            errors.append(f"sezioni critiche: {starts}")
        if S.forwarded_balances(self.outcome.lines).get(6) != 930:
            errors.append(f"saldi inoltrati {S.forwarded_balances(self.outcome.lines)}")
        if errors:
            self.fail_with_logs("una transazione per possesso", errors)


# ===================================================================================
# Comandi da tastiera via stdin rediretto (P1-I7)
# ===================================================================================

class KeyboardPipeRingTest(RingChecksMixin, _RingCase):
    """ATM3 riceve 'd 50' e 'versa 3' da una pipe prima che gli altri nodi partano; nel secondo
    giro esegue DEPOSITO 50 e il saldo finale e' 450."""

    ROUNDS = 2

    @classmethod
    def run_scenario(cls, h):
        _, ports = S.allocate_block()
        problems: list[str] = []
        atm3 = start_ring_node(h, ports, "ATM3", REFERENCE["ATM3"], 2, stdin=subprocess.PIPE)
        try:
            atm3.proc.stdin.write(b"d 50\nversa 3\n")
            atm3.proc.stdin.flush()
            atm3.proc.stdin.close()
        except OSError as exc:
            problems.append(f"scrittura su stdin di ATM3 fallita: {exc}")
        wait_started(h, "ATM3", problems)
        acc = S.wait_log(atm3.log_path, r"\bCOMANDO_ACCETTATO\b", 5.0)
        bad = S.wait_log(atm3.log_path, r"\bCOMANDO_NON_VALIDO\b", 5.0)
        extra = {"accettato": acc, "non_valido": bad}
        for node in ("ATM1", "ATM2", "ATM4"):
            start_ring_node(h, ports, node, REFERENCE[node], 2)
            wait_started(h, node, problems)
        out = collect(h, problems)
        out.extra = extra
        return out

    @classmethod
    def expected_subsequences(cls):
        queues = dict(REFERENCE_QUEUES)
        queues["ATM3"] = [("DEPOSITO", 100), ("DEPOSITO", 50)]
        return S.model_token_subsequences(queues, 2)

    def test_keyboard_events_logged(self):
        """P1-I7: 'd 50' -> COMANDO_ACCETTATO op=DEPOSITO importo=50 in_coda=2;
        'versa 3' -> COMANDO_NON_VALIDO motivo=comando_sconosciuto testo="versa 3"."""
        acc, bad = self.outcome.extra.get("accettato"), self.outcome.extra.get("non_valido")
        errors = []
        if acc is None or not acc.endswith("COMANDO_ACCETTATO     op=DEPOSITO importo=50 in_coda=2"):
            errors.append(f"COMANDO_ACCETTATO: {acc!r}")
        if bad is None or not bad.endswith(
                'COMANDO_NON_VALIDO    motivo=comando_sconosciuto testo="versa 3"'):
            errors.append(f"COMANDO_NON_VALIDO: {bad!r}")
        if errors:
            self.fail_with_logs("comandi da tastiera", errors)

    def test_keyboard_deposit_executed_in_second_round(self):
        """P1-I5, P1-I7: il DEPOSITO 50 da tastiera e' eseguito da ATM3 al possesso seq=6;
        saldo finale 450."""
        sub = S.token_subsequence_strings(self.outcome.lines.get("ATM3", []))
        errors = []
        if "INIZIO_TRANSAZIONE seq=6 saldo=400 op=DEPOSITO importo=50" not in sub:
            errors.append(f"ATM3: {sub}")
        final = S.token_subsequence_strings(self.outcome.lines.get("ATM1", []))
        if "TOKEN_RITIRATO seq=8 saldo=450 giri=2" not in final:
            errors.append(f"ATM1: {final}")
        if errors:
            self.fail_with_logs("deposito da tastiera", errors)


if __name__ == "__main__":
    unittest.main()
