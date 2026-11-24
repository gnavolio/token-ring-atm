"""Test unitari della transazione: ``Transaction``, ``apply_transaction``."""
from __future__ import annotations

import dataclasses
import unittest

import support  # noqa: F401
from tokenring.transactions import (
    DEPOSIT, MAX_AMOUNT, REASON_INSUFFICIENT_FUNDS, WITHDRAWAL, Transaction, TransactionResult,
    apply_transaction,
)


class TransactionConstantsTest(unittest.TestCase):
    def test_constants(self):
        """P1-07: costanti delle operazioni e del motivo di rifiuto."""
        self.assertEqual(DEPOSIT, "DEPOSITO")
        self.assertEqual(WITHDRAWAL, "PRELIEVO")
        self.assertEqual(MAX_AMOUNT, 1_000_000_000)
        self.assertEqual(REASON_INSUFFICIENT_FUNDS, "saldo_insufficiente")


class TransactionValidationTest(unittest.TestCase):
    def test_valid(self):
        """P1-07: deposito e prelievo con importi 1..MAX_AMOUNT."""
        self.assertEqual(Transaction(DEPOSIT, 1).amount, 1)
        self.assertEqual(Transaction(WITHDRAWAL, MAX_AMOUNT).amount, MAX_AMOUNT)
        self.assertEqual(Transaction("DEPOSITO", 100), Transaction(DEPOSIT, 100))

    def test_invalid_amounts(self):
        """P1-07, P1-12: importo 0, negativo, oltre MAX_AMOUNT, bool, float, testo -> ValueError."""
        for amount in (0, -1, -200, MAX_AMOUNT + 1, True, 1.0, 10.5, "10", None):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    Transaction(DEPOSIT, amount)  # type: ignore[arg-type]

    def test_invalid_operation(self):
        """P1-07: operazione diversa da DEPOSITO/PRELIEVO -> ValueError."""
        for op in ("VERSAMENTO", "deposito", "d", "", None):
            with self.subTest(op=op):
                with self.assertRaises(ValueError):
                    Transaction(op, 10)  # type: ignore[arg-type]

    def test_frozen(self):
        """P1-13: la richiesta e' immutabile."""
        tx = Transaction(DEPOSIT, 10)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            tx.amount = 20  # type: ignore[misc]


class ApplyTransactionTest(unittest.TestCase):
    def test_deposit(self):
        """P1-07, P1-12: deposito sempre accettato, saldo + importo."""
        self.assertEqual(apply_transaction(800, Transaction(DEPOSIT, 100)),
                         TransactionResult(True, 800, 900, None))
        self.assertEqual(apply_transaction(0, Transaction(DEPOSIT, 1)),
                         TransactionResult(True, 0, 1, None))

    def test_withdrawal_below_balance(self):
        """P1-07, P1-12: prelievo inferiore al saldo accettato."""
        self.assertEqual(apply_transaction(1000, Transaction(WITHDRAWAL, 200)),
                         TransactionResult(True, 1000, 800, None))

    def test_withdrawal_equal_to_balance(self):
        """P1-I2, P1-13: prelievo pari all'intero saldo accettato -> saldo 0."""
        self.assertEqual(apply_transaction(1100, Transaction(WITHDRAWAL, 1100)),
                         TransactionResult(True, 1100, 0, None))

    def test_withdrawal_over_balance_rejected(self):
        """P1-I2: prelievo oltre il saldo rifiutato, saldo invariato, motivo saldo_insufficiente."""
        r = apply_transaction(1000, Transaction(WITHDRAWAL, 1500))
        self.assertEqual(r, TransactionResult(False, 1000, 1000, "saldo_insufficiente"))
        self.assertEqual(r.new_balance, r.old_balance)

    def test_zero_balance_withdraw_one_rejected(self):
        """P1-I2: saldo 0, prelievo 1 -> rifiutato."""
        self.assertEqual(apply_transaction(0, Transaction(WITHDRAWAL, 1)),
                         TransactionResult(False, 0, 0, REASON_INSUFFICIENT_FUNDS))

    def test_invalid_balance(self):
        """P1-I1: saldo negativo, bool o non intero -> ValueError."""
        for bal in (-1, True, 10.0, "1000", None):
            with self.subTest(balance=bal):
                with self.assertRaises(ValueError):
                    apply_transaction(bal, Transaction(DEPOSIT, 1))  # type: ignore[arg-type]

    def test_pure_function(self):
        """P1-13: funzione pura, argomenti non modificati, stesso input -> stesso output."""
        tx = Transaction(WITHDRAWAL, 300)
        a = apply_transaction(1000, tx)
        b = apply_transaction(1000, tx)
        self.assertEqual(a, b)
        self.assertEqual(tx, Transaction(WITHDRAWAL, 300))

    def test_reference_scenario_chain(self):
        """P1-19: la catena dello scenario di riferimento produce 1000 -> 800 -> 900 -> 400."""
        balance, seen = 1000, [1000]
        for tx in (Transaction(WITHDRAWAL, 200), Transaction(DEPOSIT, 100),
                   Transaction(WITHDRAWAL, 500)):
            balance = apply_transaction(balance, tx).new_balance
            seen.append(balance)
        self.assertEqual(seen, [1000, 800, 900, 400])

    def test_refusal_scenario_chain(self):
        """P1-I2: la catena dello scenario rifiuto produce 1000 -> 1000 -> 1100 -> 0."""
        balance, seen, accepted = 1000, [1000], []
        for tx in (Transaction(WITHDRAWAL, 1500), Transaction(DEPOSIT, 100),
                   Transaction(WITHDRAWAL, 1100)):
            r = apply_transaction(balance, tx)
            accepted.append(r.accepted)
            balance = r.new_balance
            seen.append(balance)
        self.assertEqual(seen, [1000, 1000, 1100, 0])
        self.assertEqual(accepted, [False, True, True])


if __name__ == "__main__":
    unittest.main()
