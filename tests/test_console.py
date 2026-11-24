"""Test di ``tokenring/console.py`` con stdin/stdout rediretti.

Le funzioni vengono eseguite in un processo figlio: chiamarle nel processo dei test
cambierebbe il titolo e la modalita' della console di chi esegue la suite.
"""
from __future__ import annotations

import subprocess
import sys
import unittest

import support as S

CHILD = (
    "import sys\n"
    "from tokenring import console\n"
    "console.configure_stdout()\n"
    "r = console.enable_ansi()\n"
    "console.set_window_title('ATM1')\n"
    "console.disable_quick_edit()\n"
    "sys.stderr.write('RISULTATO=' + repr(r))\n"
)


class ConsoleRedirectedTest(unittest.TestCase):
    def setUp(self):
        if not (S.PACKAGE_DIR / "console.py").is_file():
            self.fail(f"codice assente: {S.PACKAGE_DIR / 'console.py'}")

    def run_child(self, stdin):
        kwargs = {"input": b""} if stdin is subprocess.PIPE else {"stdin": stdin}
        return subprocess.run([sys.executable, "-c", CHILD], cwd=str(S.ROOT),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                              env=S.child_env(), creationflags=S.CREATIONFLAGS, **kwargs)

    def test_no_exception_no_output_with_redirected_streams(self):
        """P1-I6: con stdin/stdout rediretti configure_stdout, enable_ansi,
        set_window_title e disable_quick_edit non sollevano eccezioni e non scrivono su stdout;
        enable_ansi restituisce False (stdout non e' un terminale)."""
        for label, stdin in (("DEVNULL", subprocess.DEVNULL), ("PIPE", subprocess.PIPE)):
            with self.subTest(stdin=label):
                proc = self.run_child(stdin)
                err = proc.stderr.decode("utf-8", errors="replace")
                self.assertEqual(proc.returncode, 0, err)
                self.assertEqual(proc.stdout, b"", "scrittura inattesa su stdout")
                self.assertIn("RISULTATO=False", err)


if __name__ == "__main__":
    unittest.main()
