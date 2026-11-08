"""`naga study numbers FOLDER`: every tagged number in a study's write-ups equals its source (P9, D1172).

A number is tagged by an HTML comment right after it (a % may sit between), invisible once rendered:

    The pallet limit is 30.0 <!-- N3 batch=1 --> kg.

`N3` names an entry of `numbers` in the folder's `reproduce.json`: a file (CSV or JSON, inside the
folder), the row (a 0-based index or {column: value} that matches exactly one row; `key=value` words
in the tag add to it) and the column, or for JSON a dotted `key`. The source value, times an optional
`scale`, is formatted with the entry's `format` (a Python format spec such as ",.0f") or, by default,
with as many decimals as the text shows, and must equal the written number. Untagged numbers in the
`sections` each write-up names are listed as warnings. Read-only: the check runs nothing.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .netguard import GuardError, confine_path

_NUM = r"(?<![\w.,])-?\d+(?:,\d{3})*(?:\.\d+)?(?![\w])"
# a number, an optional %, then a comment whose first word is the ID; later words are row filters
_TAG = re.compile(rf"(?P<num>{_NUM})(?P<pct>\s?%)?\s*<!--\s*(?P<id>[A-Za-z_][\w.-]*)(?P<rest>[^>]*?)\s*-->")
_BARE = re.compile(_NUM)
_COMMENT = re.compile(r"<!--.*?-->")
_CODE = re.compile(r"`[^`]*`")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST = re.compile(r"^\s*\d+[.)]\s")    # an ordered list's own numbering is not a claim


class SourceError(ValueError):
    pass


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def source_value(folder: Path, entry: dict, extra: dict[str, str]) -> tuple[float, str]:
    """(value, where) for one numbers entry plus a tag's row filters; SourceError when it does not resolve."""
    try:
        path = confine_path(str(entry.get("file") or ""), folder)
    except GuardError as exc:
        raise SourceError(str(exc)) from None
    if not entry.get("file") or not path.is_file():
        raise SourceError(f"source file {entry.get('file')!r} not found")
    rel = path.relative_to(Path(folder).resolve()).as_posix()
    if path.suffix.lower() == ".json":
        node = json.loads(path.read_text(encoding="utf-8"))
        key = str(entry.get("key") or "")
        for part in filter(None, key.split(".")):
            try:
                node = node[int(part)] if isinstance(node, list) else node[part]
            except (KeyError, IndexError, ValueError, TypeError):
                raise SourceError(f"{rel} has no key {key!r}") from None
        value, where = node, f"{rel} {key}"
    else:
        rows, col, row = _rows(path), str(entry.get("column") or ""), entry.get("row", {})
        if isinstance(row, int) and not isinstance(row, bool) and not extra:
            picked = rows[row:row + 1] if row >= 0 else []
            label = f"row {row}"
        else:
            match = {**(row if isinstance(row, dict) else {}), **extra}
            picked = [r for r in rows if all(str(r.get(k, "")).strip() == str(v) for k, v in match.items())]
            label = " ".join(f"{k}={v}" for k, v in match.items()) or "the only row"
        if len(picked) != 1:
            raise SourceError(f"{rel}: {len(picked)} rows match {label}")
        if col not in picked[0]:
            raise SourceError(f"{rel} has no column {col!r}")
        value, where = picked[0][col], f"{rel} {label}, column {col}"
    try:
        return float(value) * float(entry.get("scale", 1)), where
    except (TypeError, ValueError):
        raise SourceError(f"{where} is not a number ({value!r})") from None


def _expected(value: float, written: str, fmt: str | None) -> str:
    if fmt:
        text = format(value, fmt)
    else:
        # Half up on the decimal digits (#1700): a person writes 2.5 as 3 and 0.125 as 0.13, while
        # f-strings round the binary float half to even (2.5 -> 2, 0.125 -> 0.12).
        decimals = len(written.partition(".")[2])
        text = str(Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))
    text = text.replace(",", "")
    return "0" + text[2:] if re.fullmatch(r"-0(\.0*)?", text) else text     # -0.0 reads as 0.0


def _sections(lines: list[str], wanted: list[str]) -> list[str | None]:
    """For each line, the wanted section it falls in (a heading and everything under it), or None."""
    names = {w.strip().lower() for w in wanted}
    out, open_level, open_name = [], 0, None
    for line in lines:
        h = _HEADING.match(line)
        if h:
            level = len(h.group(1))
            if open_name is not None and level <= open_level:
                open_name = None
            if open_name is None and h.group(2).strip().lower() in names:
                open_level, open_name = level, h.group(2).strip()
            out.append(None)            # a heading's own numbers ("## 5. Results") are not claims
        else:
            out.append(open_name)
    return out


def check(folder: Path, manifest: dict | None) -> dict:
    """{"numbers": [one row per tag], "untagged": [warnings], "errors": [file-level problems]}.
    A number row: file, line, id, written, expected, where, verdict (ok, wrong, unresolved)."""
    folder = Path(folder)
    manifest = manifest or {}
    registry = manifest.get("numbers") or {}
    result = {"numbers": [], "untagged": [], "errors": []}
    writeups = manifest.get("writeups") or []
    if not writeups:
        result["errors"].append("reproduce.json names no writeups to check")
    for w in writeups:
        w = w if isinstance(w, dict) else {"file": w}
        name = str(w.get("file") or "")
        try:
            path = confine_path(name, folder)
            lines = path.read_text(encoding="utf-8").splitlines()
        except (GuardError, OSError) as exc:
            result["errors"].append(f"{name}: {exc}")
            continue
        in_section = _sections(lines, list(w.get("sections") or []))
        fenced = False
        for no, line in enumerate(lines, 1):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                continue
            text = _CODE.sub(lambda m: " " * len(m.group()), line)
            for m in _TAG.finditer(text):
                row = {"file": name, "line": no, "id": m["id"], "written": m["num"], "expected": "",
                       "where": "", "verdict": "unresolved"}
                entry = registry.get(m["id"])
                try:
                    if not isinstance(entry, dict):
                        raise SourceError(f"no entry {m['id']!r} in reproduce.json numbers")
                    extra = dict(p.split("=", 1) for p in m["rest"].split() if "=" in p)
                    value, row["where"] = source_value(folder, entry, extra)
                    row["expected"] = _expected(value, m["num"].replace(",", ""), entry.get("format"))
                    row["verdict"] = "ok" if row["expected"] == m["num"].replace(",", "") else "wrong"
                except (SourceError, OSError, ValueError) as exc:
                    row["where"] = str(exc)
                result["numbers"].append(row)
            if in_section[no - 1] is None:
                continue
            rest = _COMMENT.sub(" ", _TAG.sub(" ", text))
            rest = _LIST.sub(" ", rest)
            for m in _BARE.finditer(rest):
                result["untagged"].append({"file": name, "line": no, "section": in_section[no - 1],
                                           "number": m.group()})
    return result


def passed(result: dict) -> bool:
    return not result["errors"] and all(r["verdict"] == "ok" for r in result["numbers"])


def main_numbers(folder: Path, manifest: dict | None) -> int:
    """`naga study numbers`: one line per tagged number, then the warnings; exit 1 on any mismatch."""
    if manifest is None:
        sys.stderr.write(f"naga study numbers: no reproduce.json in {Path(folder).name}\n")
        return 2
    result = check(folder, manifest)
    for e in result["errors"]:
        print(f"ERROR {e}")
    for r in result["numbers"]:
        tail = f"source {r['expected']} ({r['where']})" if r["verdict"] != "unresolved" else r["where"]
        print(f"{r['file']}:{r['line']} {r['id']} {r['written']}: {r['verdict']}, {tail}")
    for u in result["untagged"]:
        print(f"warning {u['file']}:{u['line']} untagged {u['number']} in section {u['section']!r}")
    ok = sum(r["verdict"] == "ok" for r in result["numbers"])
    print(f"{ok} of {len(result['numbers'])} tagged numbers match; {len(result['untagged'])} untagged")
    return 0 if passed(result) else 1
