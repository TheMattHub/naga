"""Static test (#251): every problem code literal in nagasim/ and product/ is registered in
PROBLEM_CODES and follows the ^[EW]_[A-Z0-9_]+$ naming rule."""
import ast
import re
from pathlib import Path

from nagasim.core.problems import PROBLEM_CODES

_CODE = re.compile(r"^[EW]_[A-Z0-9_]+$")


def _code_literals():
    """Every string literal given as the code of a `problem(...)`/`Problem(...)`
    call -- first positional or `code=` keyword, bare name or attribute call
    (`schema.problem(...)`)."""
    found = set()
    for py_file in [p for pkg in ("nagasim", "product") for p in (Path(__file__).parent.parent / pkg).glob("**/*.py")]:
        source = py_file.read_text(encoding="utf-8")
        if "roblem(" not in source:      # one parse per file, only where a call can be
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name not in ("problem", "Problem"):
                continue
            arg = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg == "code"), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    return found


def test_every_problem_code_is_registered():
    codes = _code_literals()
    assert codes, "no problem(...) call found -- the scan is broken"
    assert sorted(c for c in codes if not _CODE.match(c)) == []
    assert sorted(codes - set(PROBLEM_CODES)) == []
