"""Static checks over the source: no function refers to a name that is defined nowhere.

2026-11-24: after the code was renamed to English, checks.check_ocr called `reading.ocr_engine()` although the module
was imported as `ocr`; the NameError was caught and the GUI showed text recognition as missing although it was
installed. No test ran that line, so this scan guards every module.
"""
import ast
import builtins
import symtable
import unittest
from pathlib import Path

PC = Path(__file__).resolve().parent.parent


def undefined_names(path: Path) -> list[str]:
    mod = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    defined = {s.get_name() for s in mod.get_symbols()
               if s.is_assigned() or s.is_imported() or s.is_namespace() or s.is_declared_global()}
    found = []

    def scan(table, where):
        for s in table.get_symbols():
            name = s.get_name()
            if not s.is_referenced() or name in defined or hasattr(builtins, name) or name in ("__file__", "__name__"):
                continue
            if s.is_global() or (table.get_type() == "module" and not s.is_assigned() and not s.is_imported()):
                found.append(f"{path.name}: {where}: {name}")
        for child in table.get_children():
            scan(child, f"{where}.{child.get_name()}")

    scan(mod, "<module>")
    return found


def undefined_in_string_annotations(path: Path) -> list[str]:
    """Names in quoted annotations ("Pack | None") that the module does not define; symtable does not see them.
    2026-11-24: three such annotations still carried the old Turkish class names (Paket, Sozluk, Tanima)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    defined |= {a.asname or a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names}
    found = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        annotations = [n.returns] + [a.annotation for a in n.args.args + n.args.kwonlyargs]
        for ann in annotations:
            if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
                for name in ast.walk(ast.parse(ann.value, mode="eval")):
                    if isinstance(name, ast.Name) and name.id not in defined and not hasattr(builtins, name.id):
                        found.append(f"{path.name}: {n.name}: {name.id}")
    return found


class UndefinedNameTest(unittest.TestCase):
    def test_no_undefined_names(self):
        files = sorted(PC.glob("*.py")) + sorted((PC / "tests").glob("*.py"))
        self.assertGreater(len(files), 20)
        problems = [p for f in files for p in undefined_names(f) + undefined_in_string_annotations(f)]
        self.assertEqual(problems, [])


class CheckOcrTest(unittest.TestCase):
    def test_reports_installed_engine(self):
        from unittest import mock

        import checks as B
        import ocr

        with mock.patch.object(ocr, "ocr_engine", return_value=object()):
            self.assertEqual(B.check_ocr().level, "ok")
        with mock.patch.object(ocr, "ocr_engine", return_value=None):
            self.assertEqual(B.check_ocr().level, "bad")


if __name__ == "__main__":
    unittest.main()
