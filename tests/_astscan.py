"""An AST scan of which files import a module, shared by the tests that pin a writer's
importers (`test_plcwrite`, `test_itemtrust`). Nothing is imported; a word in a comment or a
string is not an import."""
from __future__ import annotations

import ast
from pathlib import Path

# Imports by name (`importlib.import_module`, `__import__`) whose name is not a literal,
# reviewed one by one: {(folder/file, the name expression's source): why it may stay}.
_DYNAMIC_IMPORTS_REVIEWED = {
    ("product/cli.py", 'f"product.{module}"'): "the person's command line, product commands",
    ("product/cli.py", 'f"nagasim.{module}"'): "the person's command line: `product "
                                                 "plc-write` is a person's command, as naga's",
    ("product/callerpolicy.py", "name"): "the stem of the .py file a person picked in a dialog "
                                            "(D1123); never a model, storyboard or tool argument",
    ("product/plugin3d.py", 'f"{NAME}.{name}"'): "a module of the 3D rendering plugin, named by core code "
                                                     "(D1308); never a model, storyboard or tool argument",
    ("product/plugin3d.py", "NAME"): "the 3D rendering plugin's fixed package name (D1308)",
    ("product/film.py", "logic"): "the caller's --logic / make(logic=) module, already imported "
                                     "by run_model; hashed for the cache key (D1124)",
    # The engine's own, reviewed when test_itemtrust began scanning all of nagasim/: none of
    # them takes a module name from a model file or a tool argument, and importing a module
    # calls none of its functions.
    ("nagasim/cli.py", 'f"nagasim.{module}"'): "the person's command line: a COMMANDS row's module",
    ("nagasim/_cli.py", "module_name"): "the person's --controllers MODULE:NAME on the command line",
    ("nagasim/__init__.py", 'f"{__name__}.{attr}"'): "a lazy submodule named in _SUBMODULES",
    ("nagasim/__init__.py", "module"): "a lazy attribute's module from the fixed _LAZY table",
    ("nagasim/api.py", "name"): "the __name__ of the logic module the caller already passed",
    ("core/logic_authoring.py", "module_name"): "the fixed custom_logic module name",
    ("core/logic_authoring.py", "name"): "the caller's --logic / logic= module (D914)",
    ("readers/__init__.py", 'f"{__name__}.{kind}"'): "a reader kind from the fixed KINDS tuple",
    ("schema/layout.py", "module"): "a validator named by the fixed core/sections.py table",
}


def imports_module(path: Path, leaf: str) -> bool:
    """Whether *path* imports a module whose last name is *leaf* (`plcwrite`), by an AST walk (a word in a comment or a
    string is not an import; nothing is imported). An import by name counts too: one whose
    literal name ends in *leaf*, or whose name is not a literal at all, unless that call
    is in `_DYNAMIC_IMPORTS_REVIEWED` (Phase 15 FA)."""
    source = path.read_text("utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(a.name.split(".")[-1] == leaf for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and ((node.module or "").split(".")[-1] == leaf
                                                 or any(a.name == leaf for a in node.names)):
            return True
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in ("import_module", "__import__"):
            continue
        name = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "name"), None)
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            if name.value.split(".")[-1] == leaf:
                return True
        else:
            where = f"{path.parent.name}/{path.name}"
            if (where, ast.get_source_segment(source, name) if name else "") \
                    not in _DYNAMIC_IMPORTS_REVIEWED:
                return True
    return False
