"""Configurazione della console: colori ANSI, titolo della finestra, QuickEdit (P1-I6).

Tutte le funzioni sono "best effort": non sollevano mai eccezioni e non scrivono log. Un
terminale che non supporta una funzione deve solo perdere un abbellimento, mai fermare il
nodo. Su Windows si usa ``ctypes`` (libreria standard, nessuna dipendenza esterna)
per chiamare le API della console di ``kernel32``.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Final

_IS_WINDOWS: Final[bool] = os.name == "nt"

_STD_INPUT_HANDLE: Final[int] = -10
_STD_OUTPUT_HANDLE: Final[int] = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING: Final[int] = 0x0004
_ENABLE_QUICK_EDIT_MODE: Final[int] = 0x0040
_ENABLE_EXTENDED_FLAGS: Final[int] = 0x0080


def _kernel32() -> Any:
    """Restituisce ``kernel32`` con i tipi di argomenti e risultati dichiarati (solo Windows).

    Si usa un'istanza privata di ``WinDLL`` per non modificare ``ctypes.windll.kernel32``,
    condiviso con il resto del processo. Dichiarare ``restype = HANDLE`` evita che un
    handle a 64 bit venga troncato a ``int``.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
    kernel32.SetConsoleTitleW.restype = wintypes.BOOL
    return kernel32


def _std_handle(kernel32: Any, which: int) -> Any:
    """``GetStdHandle`` con il valore negativo convertito nel ``DWORD`` atteso dall'API."""
    return kernel32.GetStdHandle(which & 0xFFFFFFFF)


def configure_stdout() -> None:
    """Imposta ``errors="replace"`` su ``sys.stdout``: una stampa non può mai sollevare eccezioni."""
    try:
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    except Exception:  # best effort: lo stdout resta com'era
        pass


def enable_ansi() -> bool:
    """Attiva i codici colore ANSI e restituisce True se il terminale li interpreta.

    Restituisce False se lo stdout non è un terminale (pipe, file, ``DEVNULL`` dei test): in
    quel caso i colori finirebbero come byte spuri nell'output. Su Windows attiva
    ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` (0x0004) sulla console con ``SetConsoleMode``.
    """
    try:
        if sys.stdout is None or not sys.stdout.isatty():
            return False
        if not _IS_WINDOWS:
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        handle = _std_handle(kernel32, _STD_OUTPUT_HANDLE)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & _ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


def set_window_title(title: str) -> None:
    """Imposta il titolo della finestra del terminale (``ATM1``..``ATM4``).

    Il titolo associa a colpo d'occhio ogni finestra al suo nodo (P1-I6). Agisce solo se
    lo stdout è una console, con la stessa regola dei colori: se l'output è rediretto
    (pipe, file, test) il nodo potrebbe condividere la console di chi l'ha avviato e ne
    cambierebbe il titolo, che resterebbe "ATMn" anche dopo l'uscita del nodo.
    Windows: ``SetConsoleTitleW``. Altri sistemi: sequenza OSC ``ESC ] 0 ; titolo BEL``.
    """
    try:
        if sys.stdout is None or not sys.stdout.isatty():
            return
        if _IS_WINDOWS:
            _kernel32().SetConsoleTitleW(title)
        else:
            sys.stdout.write("\x1b]0;" + title + "\x07")
            sys.stdout.flush()
    except Exception:
        pass


def disable_quick_edit() -> None:
    """Disattiva la modalità QuickEdit della console di Windows.

    Perché: in conhost, con QuickEdit attivo, un clic nella finestra avvia una selezione e
    **sospende** il processo alla prima scrittura sulla console finché la selezione non viene
    annullata. Durante la demo un clic accidentale bloccherebbe il nodo, magari con il token
    in mano, e l'intero anello si fermerebbe.

    Flag: si toglie ``ENABLE_QUICK_EDIT_MODE`` (0x0040) e si imposta
    ``ENABLE_EXTENDED_FLAGS`` (0x0080), senza il quale Windows ignora la modifica di
    QuickEdit. Agisce solo su Windows e solo se lo stdin è una console interattiva: con stdin
    rediretto (``DEVNULL``/``PIPE`` dei test) non fa nulla. È best effort e silenziosa:
    nessuna eccezione e nessun log, perché un fallimento lascia solo il comportamento
    predefinito di conhost. In Windows Terminal non ha effetto (lì la selezione non blocca).
    """
    try:
        if not _IS_WINDOWS or sys.stdin is None or not sys.stdin.isatty():
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        handle = _std_handle(kernel32, _STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        kernel32.SetConsoleMode(
            handle, (mode.value & ~_ENABLE_QUICK_EDIT_MODE & 0xFFFFFFFF) | _ENABLE_EXTENDED_FLAGS)
    except Exception:
        pass
