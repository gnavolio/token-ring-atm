"""Avvio di un nodo ATM dell'anello Token Ring: ``python atm_node.py <opzioni>``.

Il controllo della versione di Python precede l'import del pacchetto ``tokenring``: il
pacchetto usa sintassi di Python 3.10/3.11 e, importato da una versione più vecchia,
fallirebbe con un errore di sintassi incomprensibile invece del messaggio chiaro qui sotto.

Alla fine si usa ``os._exit(codice)`` invece di un'uscita normale: il thread daemon della
tastiera resta bloccato in ``readline()`` su stdin e, alla chiusura ordinaria
dell'interprete, Python 3.11 può terminare con "Fatal Python error: could not acquire lock
for <stdin>". Log e stdout sono già stati svuotati, quindi ``os._exit`` non perde nulla.
"""
import os, sys  # noqa: E401 - unici import prima del controllo di versione

if sys.version_info < (3, 11):
    sys.stderr.write("Errore: e' richiesto Python 3.11 o superiore.\n")
    sys.exit(2)

if __name__ == "__main__":
    from tokenring.cli import main

    code = main(sys.argv[1:])
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)
