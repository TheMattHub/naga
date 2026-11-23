"""`naga trust`: a person at a terminal trusts a model's item code on this computer.

    naga trust MODEL            show each untrusted block of item code and ask, one by one
    naga trust --list           the item code trusted on this computer
    naga trust --remove PREFIX  stop trusting it (12 or more hex characters of its fingerprint)

SECURITY: permission is the caller's, never the file's (`netguard`). Trusting code grants
more than approving one setpoint (`naga approve`), so this is stricter on purpose: the
whole code is shown, the question says it runs unsandboxed, only a typed y or yes trusts,
end of input is no, input that is not a terminal is refused before anything is asked (a
piped "y" from a script, an agent's shell or CI), and there is no flag that answers for
the person. The fingerprint is computed from the code shown; one in the file is never read.
"""
from __future__ import annotations

import sys

from nagasim.core import itemlogic
from nagasim.settings import itemtrust, trustwrite
from nagasim.settings.trusted import StoreUnreadable

__all__ = ["trust_model", "main"]

QUESTION = "This code runs on this computer with your permissions; Naga does not sandbox it. Trust it? [y/N] "


def _shown(text) -> str:
    """*text* from the model file as printable ASCII: anything else as its escape (`\\x1b`,
    `\\u202e`, `\\xe9`), so a terminal escape or a bidi control cannot change what is read."""
    # #1997: a raw ESC in a comment could erase the line that runs.
    return "".join(c if c == "\t" or " " <= c <= "~" else c.encode("unicode_escape").decode("ascii")
                   for c in str(text))


def trust_model(path, *, by: str, ask=input, isatty=None, out=print) -> int:
    """Ask about each untrusted block of item code in the model at *path*; 0 when every block
    is trusted at the end, 1 otherwise."""
    from nagasim.core.schema import load_layout

    blocks = itemlogic.untrusted_blocks(load_layout(path))
    if not blocks:
        out("Every block of item code in this model is trusted on this computer.")
        return 0
    if isatty is None:      # read when asked: the stdin of this moment
        isatty = lambda: sys.stdin is not None and sys.stdin.isatty()   # noqa: E731
    try:
        terminal = bool(isatty())
    except (OSError, ValueError):
        terminal = False
    if not terminal:
        out("E_TRUST_TERMINAL: trusting code needs a person at a terminal; this input is not one")
        return 1
    left = len(blocks)
    for entry in blocks:
        # Split as Python reads lines (normalised: "\n" only); splitlines() also breaks at
        # characters Python does not, so the numbers would not match a traceback's.
        lines = itemtrust.normalise(entry["source"]).rstrip("\n").split("\n")
        out(f"Item {_shown(entry['definition'])}, class {_shown(entry['class'])}")
        out(_shown(itemlogic.claim_line(entry)))
        out(f"Fingerprint {entry['fingerprint']} ({itemtrust.short(entry['fingerprint'])}), {len(lines)} lines:")
        width = len(str(len(lines)))
        for number, line in enumerate(lines, 1):
            out(f"{number:>{width}} | {_shown(line)}")
        hidden = itemtrust.hidden_character(entry["source"])
        if hidden:          # never asked: what was shown is not what runs
            out(f"E_TRUST_HIDDEN: this code holds a character that shows otherwise than it runs ({hidden}); "
                "not trusted")
            continue
        try:
            answer = ask(QUESTION)
        except (EOFError, OSError):         # no one to answer: not trusted, and nothing more is asked
            out("Not trusted.")
            break
        if str(answer).strip().lower() not in ("y", "yes"):
            out("Not trusted.")
            continue
        try:
            trustwrite.trust_source(entry["source"], label=f"{entry['class']} in {entry['definition']}", by=by)
        except (StoreUnreadable, ValueError) as exc:
            out(f"E_TRUST_STORE: {exc}")
            return 1
        out(f"Trusted {itemtrust.short(entry['fingerprint'])}.")
        left -= 1
    return 0 if left == 0 else 1


def main(argv=None) -> int:
    import argparse
    import getpass

    from nagasim._cli import setup_cli

    setup_cli()
    out = print
    parser = argparse.ArgumentParser(
        prog="naga trust", allow_abbrev=False,
        description="Trust a model's item code on this computer. It shows each block of code that "
                    "is not trusted yet, where the model file says it comes from and its "
                    "fingerprint, and asks; only a typed y trusts it. Trusted code runs with your "
                    "permissions, unsandboxed, in your runs, experiments and assistant tool calls. "
                    "Any change to the code needs your yes again.")
    parser.add_argument("model", nargs="?", default=None, metavar="MODEL", help="the model file")
    parser.add_argument("--list", action="store_true", help="list the item code trusted on this computer")
    parser.add_argument("--remove", metavar="PREFIX",
                        help="stop trusting the code whose fingerprint starts with PREFIX (12 or more hex characters)")
    args = parser.parse_args(argv)
    if sum((args.model is not None, args.list, args.remove is not None)) != 1:
        parser.error("give a MODEL, --list or --remove PREFIX")
    if args.list:
        problem = itemtrust.store_problem()
        if problem:
            out(f"E_TRUST_STORE: {itemtrust.store_path()} does not read: {problem}")
            return 1
        found = itemtrust.entries()
        for entry in found:
            out(_shown(f"{itemtrust.short(entry.fingerprint)}  {entry.label}  {entry.by}  {entry.at}"))
        if not found:
            out("No item code is trusted on this computer.")
        return 0
    if args.remove is not None:
        try:
            gone = trustwrite.revoke(args.remove)
        except (StoreUnreadable, ValueError) as exc:
            out(f"E_TRUST_STORE: {exc}")
            return 1
        for fp in gone:
            out(f"No longer trusted: {itemtrust.short(fp)}")
        if not gone:
            out(f"No trusted item code starts with {args.remove}.")
        return 0 if gone else 1
    try:
        by = getpass.getuser()
    except (OSError, KeyError, ImportError):
        by = "terminal"
    return trust_model(args.model, by=by)
