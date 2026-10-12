"""Helpers shared by the validators of the schema package: link pairs, the
expression check every section reuses, and the distribution/time-unit checks
of a duration spec."""
from __future__ import annotations

from nagasim.core import agents, sd
from nagasim.core.distributions import (DISTRIBUTION_REGISTRY, distribution_mean,
                                        distribution_needs_finite_mean,
                                        get_available_distributions)
from nagasim.core.expressions import (SD_MACROS, SD_VALUE_MACROS, ExpressionError,
                                      compile_expression)
from nagasim.core.problems import problem


def _known_distributions() -> set[str]:
    """get_available_distributions() with no block_type returns the union of
    every catalogue list -- every registered distribution name, built-in or
    plugin (ROADMAP 2.4 A/B). Computed fresh on each call, not cached at
    import time, so a distribution a plugin registers after this module
    loads is recognized as soon as nagasim.plugins.load_plugins() runs."""
    return set(get_available_distributions())


def _link_pairs(layout: dict) -> list[tuple[str, str]]:
    """Every link as (source, target): accepts both the v7 `[src, tgt]` pair
    and the v8 `{"source", "target", ...}` object, so a caller need not migrate
    a hand-built layout first (many tests exercise validate_* directly)."""
    pairs = []
    for link in layout.get("links", []):
        if isinstance(link, dict):
            pairs.append((link.get("source"), link.get("target")))
        else:
            pairs.append((link[0], link[1]))
    return pairs


def _dist_hint() -> str:
    return f"use one of: {', '.join(sorted(_known_distributions()))}"


def _check_deterministic_mean(problems: list, deterministic: bool, dist, dist_params: dict,
                              label: str, path: str, block: str | None = None) -> None:
    """PHASE6 W4 P1, D119: in deterministic mode, every duration-bearing
    distribution must have a finite mean for its own params -- checked once
    here, reused by the CREATE/DELAY/SEIZE/... block loop, `_validate_failures`
    and `_validate_link_delays`. Skipped for a distribution this build does
    not know at all (E_UNKNOWN_DISTRIBUTION already covers that) or one that
    is not checked for a mean in the first place (CustomExpression,
    FixedSchedule -- `distribution_needs_finite_mean`). Also skipped when any
    param is a STRING expression (DEVLOG #196, e.g. an order's own service
    time riding along as `entity.svc`, ARCHITECTURE 5.16/5.31): this check has
    no entity/ctx scope to resolve it against, so it cannot know the mean
    ahead of time -- `StochasticDistribution.sample(..., deterministic=True)`
    resolves it per entity at run time and raises there if that instance
    truly has no finite mean."""
    if not deterministic or dist not in _known_distributions() or not distribution_needs_finite_mean(dist):
        return
    # No `mean` at all (a plugin's, #207) fails whatever the params are.
    has_mean = DISTRIBUTION_REGISTRY[dist].mean is not None
    if has_mean and any(isinstance(v, str) for v in (dist_params or {}).values()):
        return
    if not has_mean or distribution_mean(dist, dist_params or {}) is None:
        problems.append(problem(
            "E_DETERMINISTIC_NO_MEAN",
            f"{label}: {dist} has no finite mean for {dist_params!r} in deterministic mode",
            path, block=block,
            hint="use a distribution/params with a finite mean, or turn off simulation.deterministic"))


def _check_expression(problems: list, block_name: str, text, variables: set[str],
                      path: str = "", tables: dict | None = None,
                      refs: dict | None = None, allow_self: bool = False) -> None:
    try:
        expression = compile_expression(text, variables)
    except ExpressionError as exc:
        problems.append(problem("E_EXPRESSION", f"{block_name}: {text}: {exc}", path,
                                block=block_name,
                                hint="see the expression reference; names allowed are entity.<attr>, "
                                     "now/TNOW, declared variables and the built-in functions"))
        return
    if not allow_self and "self" in expression.names:      # PHASE5 AB-1
        problems.append(problem("E_EXPRESSION", f"{block_name}: {text}: self.<attribute> is only "
                                f"available in an agent type's expressions", path,
                                block=block_name,
                                hint="read agents from here with COUNT/SUM/MEAN(\"population\", ...)"))
    # Inline delay shorthand and PHASE6 S2 value macros left after `sd.expand`
    # sit where no hidden record can be generated: a block, a price, a
    # TRANSFER amount.
    for name in sorted(expression.calls & (set(SD_MACROS) | set(SD_VALUE_MACROS))):
        problems.append(problem(
            "E_SD_FUNCTION_CONTEXT", f"{block_name}: {text}: {name}() cannot be used here", path,
            block=block_name,
            hint="write it in a level's initial value, a flow rate, an auxiliary or a delay "
                 "field, and read the result here with AUX() or LEVEL()/DELAY()"))
    # SUM/MEAN left after `sd.expand` (PHASE5 SD-3): not in an SD section, no
    # `[*]` to unroll over, or the agent form.
    if expression.calls & set(sd.AGGREGATES):
        for name, count in sd.leftover_aggregates(text):
            if count >= 2:
                continue        # the agent form (PHASE5 AB-1/AB-4), checked just below
            else:
                problems.append(problem(
                    "E_SUBSCRIPT_AGGREGATE", f"{block_name}: {text}: {name}(x) needs a \"[*]\" "
                    f"over a subscripted record in x, inside a level, flow, auxiliary or delay",
                    path, block=block_name,
                    hint=f"{name}(\"Population\", \"attribute\") over agents" if allow_self else
                    f"{name}(LEVEL(\"Pop[*]\")) in an auxiliary, read here with AUX()"))
        for name in agents.bad_aggregates(text):
            problems.append(problem(
                "E_AGGREGATE_AGENTS", f"{block_name}: {text}: {name}(population, attribute[, state]) "
                f"takes literal names", path, block=block_name,
                hint=f"{name}(\"People\", \"age\"); the system-dynamics form is "
                     f"{name}(LEVEL(\"x[*]\"))"))
    if tables is None:
        return
    # TABLE("name", ...) with a literal name (PHASE4 A1): a computed name can
    # only be checked at run time, and is left to the evaluator.
    for name, keys in expression.tables:
        if name not in tables:
            problems.append(problem(
                "E_TABLE_UNKNOWN", f"{block_name}: {text}: unknown table '{name}'", path,
                block=block_name,
                hint=f"declare it under \"tables\"; declared: "
                     f"{', '.join(sorted(tables)) or '(none)'}"))
        elif tables[name] != keys:
            problems.append(problem(
                "E_TABLE_KEYS",
                f"{block_name}: {text}: table '{name}' takes {tables[name]} key(s), got {keys}",
                path, block=block_name,
                hint=f"TABLE(\"{name}\", {', '.join(f'k{i + 1}' for i in range(tables[name]))})"))
    # TS("name") / LEVEL("name") / FLOW("name") with a literal name (PHASE4
    # D1/D2), checked against the declared sections the same way.
    for func, (declared, code, section, label) in (refs or {}).items():
        for name in expression.refs.get(func, ()):
            if name not in declared:
                problems.append(problem(
                    code, f"{block_name}: {text}: unknown {label} '{name}'", path,
                    block=block_name,
                    hint=f"declare it under \"{section}\"; declared: "
                         f"{', '.join(sorted(declared)) or '(none)'}"))
    pops = (refs or {}).get("COUNT", (None,))[0]
    if isinstance(pops, dict):
        for code, message, hint in agents.literal_problems(text, pops):
            problems.append(problem(code, f"{block_name}: {text}: {message}", path,
                                    block=block_name, hint=hint))
