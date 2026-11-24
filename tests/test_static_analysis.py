"""Analisi statica del codice di produzione con ``ast`` (non lo esegue).

Verifica: solo libreria standard; P1-10 (nessuna memoria condivisa, saldo mai scritto su file);
P1-09 (nessuna porta derivata dall'ID); la struttura di ``atm_node.py``; i file di progetto con
contenuto fissato (``.gitignore``).
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

import support as S

FORBIDDEN_MODULES = {"mmap", "multiprocessing", "_multiprocessing", "sqlite3", "shelve",
                     "pickle", "_pickle", "dbm"}
EXPECTED_MODULES = ("__init__", "protocol", "transactions", "logfmt", "console", "config",
                    "node", "cli")
WRITE_MODE_CHARS = set("wax+")


def production_files() -> list[Path]:
    files = sorted(S.PACKAGE_DIR.glob("*.py"))
    if S.ATM_NODE.is_file():
        files.append(S.ATM_NODE)
    return files


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.AST) -> list[tuple[str, int, int]]:
    """(nome completo, livello relativo, riga) di ogni import."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((a.name, 0, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.append((node.module or "", node.level, node.lineno))
    return out


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        base = f.value
        prefix = base.id + "." if isinstance(base, ast.Name) else ""
        return prefix + f.attr
    return ""


def _open_kind(call: ast.Call) -> int | None:
    """Posizione dell'argomento 'mode' se la chiamata apre un file, altrimenti None.
    open()/io.open()/codecs.open(): mode in posizione 1; metodo .open() (Path.open): posizione 0."""
    f = call.func
    if isinstance(f, ast.Name) and f.id == "open":
        return 1
    if isinstance(f, ast.Attribute) and f.attr == "open":
        if isinstance(f.value, ast.Name) and f.value.id in ("io", "codecs"):
            return 1
        if isinstance(f.value, ast.Name) and f.value.id in ("os", "webbrowser", "socket"):
            return None
        return 0
    return None


def _mode_of_open(call: ast.Call, pos: int) -> str | None:
    """Modo di una chiamata open(...) se e' una costante, '' se assente, None se non deducibile."""
    mode_node = None
    if len(call.args) > pos:
        mode_node = call.args[pos]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    if mode_node is None:
        return ""
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return mode_node.value
    return None


class ProductionCodeStaticTest(unittest.TestCase):
    def setUp(self):
        if not S.PACKAGE_DIR.is_dir():
            self.fail(f"codice assente: {S.PACKAGE_DIR}")
        self.files = production_files()

    def rel(self, p: Path) -> str:
        return str(p.relative_to(S.ROOT)).replace("\\", "/")

    def test_expected_modules_exist(self):
        """Il pacchetto tokenring contiene i moduli previsti e atm_node.py esiste."""
        names = {p.stem for p in S.PACKAGE_DIR.glob("*.py")}
        for m in EXPECTED_MODULES:
            self.assertIn(m, names, f"manca tokenring/{m}.py")
        self.assertTrue(S.ATM_NODE.is_file(), "manca atm_node.py")

    def test_only_standard_library_imports(self):
        """Ogni import e' della libreria standard (sys.stdlib_module_names) o
        interno al pacchetto tokenring."""
        bad = []
        for path in self.files:
            for name, level, line in imported_modules(parse(path)):
                top = name.split(".")[0]
                if level > 0 or top == "tokenring" or top == "__future__":
                    continue
                if top not in sys.stdlib_module_names:
                    bad.append(f"{self.rel(path)}:{line} import {name}")
        self.assertEqual(bad, [], "import non della libreria standard")

    def test_no_shared_memory_or_persistence_modules(self):
        """P1-10: niente mmap, multiprocessing, sqlite3, shelve, pickle, dbm."""
        bad = []
        for path in self.files:
            for name, level, line in imported_modules(parse(path)):
                if level == 0 and name.split(".")[0] in FORBIDDEN_MODULES:
                    bad.append(f"{self.rel(path)}:{line} import {name}")
        self.assertEqual(bad, [], "moduli di memoria condivisa/persistenza vietati")

    def test_ctypes_only_in_console(self):
        """P1-10: ctypes (che permetterebbe memoria condivisa) solo in console.py."""
        bad = []
        for path in self.files:
            if path.name == "console.py":
                continue
            for name, level, line in imported_modules(parse(path)):
                if level == 0 and name.split(".")[0] == "ctypes":
                    bad.append(f"{self.rel(path)}:{line}")
        self.assertEqual(bad, [])

    def test_file_io_confined(self):
        """P1-10: il saldo non viene mai scritto su file. Le sole aperture in scrittura
        sono quelle del file di log (logfmt.py); le sole letture di file sono quelle
        dello scenario (transactions.py). Vietati os.open/os.write, json.dump, write_text,
        write_bytes."""
        bad = []
        for path in self.files:
            rel = self.rel(path)
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                short = name.split(".")[-1]
                where = f"{rel}:{node.lineno} {name}()"
                if name in ("os.open", "os.write", "os.fdopen", "json.dump", "pickle.dump",
                            "marshal.dump") or short in ("write_text", "write_bytes"):
                    bad.append(where + " vietata")
                elif short in ("read_text", "read_bytes"):
                    if path.name != "transactions.py":
                        bad.append(where + " lettura di file fuori da transactions.py")
                elif (pos := _open_kind(node)) is not None:
                    mode = _mode_of_open(node, pos)
                    writes = mode is None or bool(set(mode) & WRITE_MODE_CHARS)
                    if writes and path.name != "logfmt.py":
                        bad.append(where + f" in scrittura (modo {mode!r}) fuori da logfmt.py")
                    if not writes and path.name != "transactions.py":
                        bad.append(where + f" in lettura (modo {mode!r}) fuori da transactions.py")
        self.assertEqual(bad, [], "I/O su file non previsto dal design")

    def test_no_ports_derived_from_ids(self):
        """P1-09: nel codice non compaiono le porte della demo (5000-5004)."""
        bad = []
        for path in self.files:
            for node in ast.walk(parse(path)):
                if (isinstance(node, ast.Constant) and type(node.value) is int
                        and 5000 <= node.value <= 5004):
                    bad.append(f"{self.rel(path)}:{node.lineno} costante {node.value}")
        self.assertEqual(bad, [], "porte/topologia nel codice")

    def test_future_annotations_first(self):
        """Regola comune: ogni modulo di tokenring inizia con
        'from __future__ import annotations' (dopo l'eventuale docstring)."""
        bad = []
        for path in sorted(S.PACKAGE_DIR.glob("*.py")):
            body = parse(path).body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            first = body[0] if body else None
            ok = (isinstance(first, ast.ImportFrom) and first.module == "__future__"
                  and any(a.name == "annotations" for a in first.names))
            if not ok:
                bad.append(self.rel(path))
        self.assertEqual(bad, [])

    def test_package_version(self):
        """tokenring/__init__.py definisce __version__ = "1.0"."""
        tree = parse(S.PACKAGE_DIR / "__init__.py")
        values = [n.value.value for n in tree.body
                  if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                  and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets)]
        self.assertEqual(values, ["1.0"])


class AtmNodeEntryPointTest(unittest.TestCase):
    def setUp(self):
        if not S.ATM_NODE.is_file():
            self.fail(f"codice assente: {S.ATM_NODE}")
        self.source = S.ATM_NODE.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_structure(self):
        """Punto d'ingresso: solo 'import os, sys' a livello di modulo; controllo della versione
        (sys.version_info) PRIMA dell'import di tokenring, che sta dentro
        'if __name__ == "__main__":'; uscita con os._exit."""
        top_imports = [n for n in self.tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = sorted(a.name for n in top_imports for a in n.names)
        self.assertTrue(all(isinstance(n, ast.Import) for n in top_imports), names)
        self.assertEqual(sorted(set(names)), ["os", "sys"])

        imports_tokenring = [n for n in ast.walk(self.tree)
                             if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("tokenring")]
        self.assertEqual(len(imports_tokenring), 1, "atteso un solo 'from tokenring.cli import main'")
        imp = imports_tokenring[0]
        self.assertEqual(imp.module, "tokenring.cli")
        self.assertEqual([a.name for a in imp.names], ["main"])

        main_ifs = [n for n in self.tree.body if isinstance(n, ast.If)
                    and "__name__" in ast.unparse(n.test) and "__main__" in ast.unparse(n.test)]
        self.assertTrue(main_ifs, "manca il blocco if __name__ == '__main__'")
        self.assertTrue(any(imp in list(ast.walk(b)) for b in main_ifs),
                        "l'import di tokenring deve stare dentro if __name__ == '__main__'")

        version_lines = [n.lineno for n in ast.walk(self.tree)
                         if isinstance(n, ast.Attribute) and n.attr == "version_info"]
        self.assertTrue(version_lines, "manca il controllo sys.version_info")
        self.assertLess(min(version_lines), imp.lineno,
                        "il controllo di versione deve precedere l'import di tokenring")
        tuples = [[getattr(e, "value", None) for e in n.elts] for n in ast.walk(self.tree)
                  if isinstance(n, ast.Tuple)]
        self.assertIn([3, 11], tuples, "il controllo di versione deve usare (3, 11)")
        self.assertTrue(any(isinstance(n, ast.Attribute) and n.attr == "_exit"
                            for n in ast.walk(self.tree)), "manca os._exit")


class ProjectFilesTest(unittest.TestCase):
    def test_gitignore_content(self):
        """Contenuto esatto di .gitignore (log e bytecode esclusi dal repository)."""
        path = S.ROOT / ".gitignore"
        self.assertTrue(path.is_file(), f"manca {path}")
        self.assertEqual(path.read_text(encoding="utf-8").splitlines(),
                         ["# Log generati a ogni esecuzione dai nodi (vedi README)", "logs/",
                          "# Bytecode Python", "__pycache__/", "*.pyc"])

    def test_no_init_in_tests(self):
        """La cartella tests/ non contiene __init__.py (moduli di supporto importati per nome)."""
        self.assertFalse((S.ROOT / "tests" / "__init__.py").exists())


if __name__ == "__main__":
    unittest.main()
