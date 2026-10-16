"""Build a model in Python: a thin layer over the model JSON (D475; provisional until 1.0,
it may change between minor versions).

    from nagasim import build as nb

    m = nb.Model(simulation={"time_limit": 1000, "warmup": 100, "replications": 5})
    m.add_resource(name="Server")
    arrive = m.create(distribution="Exponential", dist_params={"rate": 1.0})
    serve = m.process(resource_name="Server", distribution="Exponential",
                      dist_params={"rate": 1.25})
    arrive >> serve >> m.dispose()
    m.save("mm1.json")

Nothing here is written per block or per section. A block method (`m.process`, ...) exists
for every type in `BLOCK_REGISTRY`, plugins included, and takes its parameters by name; an
`add_<item>` method (`m.add_resource`, ...) exists for every list in
`sections.DOCUMENT_LISTS`; every other top-level section is a keyword of `Model`. The builder
only writes the dict a file holds (`m.layout`); `to_dict` is that dict in `naga fmt`'s form,
`save` writes it to the path given (the only file the builder writes) and `run` hands it to
`api.run_model`. What a value means is the engine's, as for a file.

`export_python` (`naga export --python`) writes the builder code of any model, so that
running it gives `naga fmt`'s form of the model back.
"""
from __future__ import annotations

import argparse
import inspect
import keyword
import re
import sys
from pathlib import Path

from nagasim import modelfmt
from nagasim.core import sections
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.schema import CURRENT_VERSION

__all__ = ["Model", "Block", "export_python", "main"]

_RECORDS = {f"add_{s.item}": s for s in sections.DOCUMENT_LISTS}
_TOP = {s.name for s in sections.SECTIONS} - {"blocks", "links", "submodels"} - {
    s.name for s in sections.DOCUMENT_LISTS}


class Block:
    """A block placed by a block method: its name, and `>>` to link it on."""

    __slots__ = ("level", "name")

    def __init__(self, level: "Level", name: str):
        self.level, self.name = level, name

    def __rshift__(self, other):
        return self.level.link(self, other)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Block({self.name!r})"


def _block_method(level: "Level", spec):
    names = {p.name for p in spec.params}
    allowed = names | set(modelfmt.BLOCK_KEYS)

    def add(name=None, /, **fields):
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise TypeError(f"{spec.type_name.lower()}() got unknown keyword(s) {unknown}; "
                            f"its parameters: {sorted(names)}")
        name = name or level._next_name(spec.type_name)
        block = {"name": name, "type": spec.type_name}
        block.update({k: fields.pop(k) for k in modelfmt.BLOCK_KEYS if k in fields})
        block["params"] = fields
        level._data["blocks"].append(block)
        return Block(level, name)

    add.__name__ = add.__qualname__ = spec.type_name.lower()
    summary = " ".join((spec.cls.__doc__ or "").split())
    add.__doc__ = "\n".join([f"Add a {spec.type_name} block. {summary}", ""] + [
        f"{p.name}={p.default!r}{' (required)' if p.required else ''}: {p.description}"
        for p in spec.params])
    kw = inspect.Parameter.KEYWORD_ONLY
    add.__signature__ = inspect.Signature(
        [inspect.Parameter("name", inspect.Parameter.POSITIONAL_ONLY, default=None)]
        + [inspect.Parameter(k, kw, default=None) for k in modelfmt.BLOCK_KEYS]
        + [inspect.Parameter(p.name, kw, default=p.default) for p in spec.params
           if p.name.isidentifier() and not keyword.iskeyword(p.name)])
    return add


class Level:
    """One canvas: the model's own, or a submodel definition's (`Model.definition`)."""

    def __init__(self, record: dict):
        # A (plugin) block type named like a builder method would be silently unreachable.
        hidden = sorted({t.lower() for t in BLOCK_REGISTRY} & (set(dir(type(self))) | set(_RECORDS) | {"layout"}))
        if hidden:
            raise TypeError(f"block type(s) {hidden} have the name of a builder method; "
                            "rename the plugin's block type to build with it")
        self._data = record
        record.setdefault("blocks", [])
        record.setdefault("links", [])

    def __getattr__(self, attr: str):
        spec = BLOCK_REGISTRY.get(attr.upper()) if attr.islower() else None
        if spec is None:
            raise AttributeError(f"{type(self).__name__!r} has no attribute {attr!r}")
        return _block_method(self, spec)

    def __dir__(self):
        return sorted(set(super().__dir__()) | {t.lower() for t in BLOCK_REGISTRY})

    def _next_name(self, type_name: str) -> str:
        taken = {b.get("name") for b in self._data["blocks"]}
        n = 1
        while f"{type_name.lower()}_{n}" in taken:
            n += 1
        return f"{type_name.lower()}_{n}"

    def link(self, source, target, **fields):
        """Link *source* to *target* (blocks or names); *fields* are the link's own
        (`delay`, `source_port`, `target_port`). Returns *target*, so links chain; a name
        ends a chain."""
        if any(isinstance(end, Block) and end.level is not self for end in (source, target)):
            raise ValueError(f"link {source} -> {target}: both blocks must be on this level "
                             "(a submodel definition links only its own blocks)")
        self._data["links"].append({"source": str(source), "target": str(target), **fields})
        return target


class Model(Level):
    """A model. Keywords are top-level sections (`simulation`, `info`, `network`, ...)."""

    def __init__(self, **top):
        unknown = sorted(set(top) - _TOP)
        if unknown:
            raise TypeError(f"Model() got unknown section(s) {unknown}; lists are added with "
                            f"{sorted(_RECORDS)}")
        self.layout = {"schema_version": CURRENT_VERSION, **top}
        super().__init__(self.layout)

    def __getattr__(self, attr: str):
        section = _RECORDS.get(attr)
        if section is None:
            return super().__getattr__(attr)

        def add(**fields):
            self.layout.setdefault(section.name, []).append(fields)
            return fields.get(section.key)

        add.__name__ = attr
        add.__doc__ = f"Add one entry to `{section.name}` and return its {section.key}. {section.doc}"
        return add

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(_RECORDS))

    def definition(self, name: str, **fields) -> Level:
        """A submodel definition (`info`, `parameters`, `scoped`, ...): a Level with the same
        block methods. An instance of it is `m.submodel(definition=name, ...)`."""
        definition = dict(fields)
        self.layout.setdefault("submodels", {})[name] = definition
        return Level(definition)

    def to_dict(self) -> dict:
        """The model in `naga fmt`'s form."""
        return modelfmt.minimal(self.layout)

    def save(self, path) -> Path:
        """Write the model to *path*, and nothing else; returns the path."""
        path = Path(path)
        path.write_text(modelfmt.dumps(self.to_dict()), encoding="utf-8", newline="\n")
        return path

    def run(self, **options):
        """`api.run_model(self.to_dict(), **options)`."""
        from nagasim import api

        return api.run_model(self.to_dict(), **options)


# ------------------------------------------------------------------ export
def _call(target: str, func: str, args: list[str], kwargs: dict) -> str:
    """`target = func(args, k=v, **{odd: v})` on one line, or one argument a line."""
    items = list(args)
    for k, v in kwargs.items():     # in order: the order is the file's
        if k.isidentifier() and not keyword.iskeyword(k):
            items.append(f"{k}={modelfmt.render(v, 4, len(k) + 5, repr, 4)}")
        else:                       # "from", a key that is no Python name
            items.append(f"**{{{k!r}: {modelfmt.render(v, 4, len(k) + 10, repr, 4)}}}")
    head = f"{target} = {func}(" if target else f"{func}("
    line = head + ", ".join(items) + ")"
    if len(line) <= modelfmt.WIDTH and "\n" not in line:
        return line
    return head + "\n" + "".join(f"    {item},\n" for item in items) + ")"


def export_python(layout) -> str:
    """Builder code for *layout* (a path or a dict): running it gives `m`, whose `to_dict()`
    is `naga fmt`'s form of the model. Every value is written as a Python literal (`repr`):
    no text of the model ever becomes code."""
    short = modelfmt.minimal(layout)
    top = {k: v for k, v in short.items() if k in _TOP}
    lines = ["from nagasim import build as nb", "", _call("m", "nb.Model", [], top)]
    for section in sections.DOCUMENT_LISTS:
        lines += [_call("", f"m.add_{section.item}", [], record)
                  for record in short.get(section.name) or ()]
    taken = {"m", "nb"}

    def variable(name: str) -> str:
        base = re.sub(r"\W", "_", str(name)) or "block"
        base = base if base[0].isalpha() and not keyword.iskeyword(base) else f"b_{base}"
        found, n = base, 2
        while found in taken:
            found, n = f"{base}_{n}", n + 1
        taken.add(found)
        return found

    def level(owner: str, record: dict) -> None:
        names = {}
        for block in record.get("blocks") or ():
            # The type is the one value written as code (an attribute name), not via repr:
            # only a registered type, which is a plain name, gets through (#497).
            kind = str(block.get("type", ""))
            if kind.upper() not in BLOCK_REGISTRY or not kind.isidentifier():
                raise ValueError(f"export: block {block.get('name')!r} has type {kind!r}, "
                                 "not a registered block type")
            names[block["name"]] = var = variable(block["name"])
            fields = {k: block[k] for k in modelfmt.BLOCK_KEYS if k in block}
            lines.append(_call(var, f"{owner}.{kind.lower()}", [repr(block["name"])],
                               {**fields, **(block.get("params") or {})}))
        # Links in file order (it is the branch order): runs of plain links, each from the
        # block the last one reached, as `a >> b >> c`; any other link as `link(...)`.
        chain: list[str] = []
        for link in record.get("links") or ():
            source, target = link.get("source"), link.get("target")
            extra = {k: v for k, v in link.items() if k not in ("source", "target")}
            if not extra and source in names and target in names:
                if not (chain and chain[-1] == names[source]):
                    lines.extend([" >> ".join(chain)] if chain else [])
                    chain = [names[source]]
                chain.append(names[target])
                continue
            lines.extend([" >> ".join(chain)] if chain else [])
            chain = []
            ends = [names.get(source) or repr(source), names.get(target) or repr(target)]
            lines.append(_call("", f"{owner}.link", ends, extra))
        lines.extend([" >> ".join(chain)] if chain else [])

    for name, definition in (short.get("submodels") or {}).items():
        var = variable(name)
        fields = {k: v for k, v in definition.items() if k not in ("blocks", "links")}
        lines += ["", _call(var, "m.definition", [repr(name)], fields)]
        level(var, definition)
    lines.append("")
    level("m", short)
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    """`naga export --python FILE [-o OUT]`."""
    from nagasim.modelschema import _bundled

    parser = argparse.ArgumentParser(
        prog="naga export",
        description="Write a model as code that builds it again (nagasim.build). Running the "
                    "code gives the model in the form `naga fmt` writes.")
    parser.add_argument("model", type=_bundled,
                        help="path to the model JSON, or a bundled model's name (naga examples)")
    parser.add_argument("--python", action="store_true", required=True,
                        help="Python code using nagasim.build (the one format so far)")
    parser.add_argument("-o", "--out", help="write the code here instead of printing it")
    args = parser.parse_args(argv)
    code = export_python(args.model)
    if args.out:
        Path(args.out).write_text(code, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(code)
    return 0
