"""Pacchetto ``tokenring``: nodo ATM di un anello Token Ring su 127.0.0.1.

Quattro processi indipendenti (ATM1..ATM4) condividono un unico conto bancario senza
alcuna memoria condivisa: il saldo viaggia solo dentro il messaggio ``TOKEN``, che passa
da un nodo al suo successore lungo l'anello ATM1 -> ATM2 -> ATM3 -> ATM4 -> ATM1.
Solo il nodo che ha il token in mano può eseguire una transazione (mutua esclusione
distribuita ottenuta esclusivamente con il Token Ring).

Moduli:
- ``protocol``: il messaggio TOKEN (dataclass, codifica e decodifica JSON su una riga);
- ``transactions``: transazioni (funzione pura), file di scenario e comandi da tastiera;
- ``logfmt``: elenco chiuso degli eventi di log, formattazione, colori, ``NodeLogger``;
- ``console``: colori ANSI, titolo della finestra e QuickEdit su Windows;
- ``config``: opzioni da riga di comando, ``NodeConfig``, codici d'uscita;
- ``node``: ``AtmNode``, cioè thread, macchina a stati e rete;
- ``cli``: ``main()``, sequenza di avvio e arresto del processo.
"""

from __future__ import annotations

__version__ = "1.0"
