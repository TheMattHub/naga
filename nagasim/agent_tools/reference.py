"""Reference tools (block types, distributions, the expression language) and model
validation (the engine half of the former docs tools).
"""
from __future__ import annotations

from nagasim.core import schema as schema_mod
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.distributions import DISTRIBUTION_REGISTRY, get_available_distributions
from nagasim.core.expressions import FUNCTIONS, RESERVED_NAMES

from .core import MAX_PROBLEMS, ToolError, _document_args, _problem_dicts, tool, workspace

# ============================================================ reference tools
@tool(read_only=True)
def list_block_types() -> dict:
    """List every block type this build can place in a model, with a one-line
    summary and its required parameters.

    Use this FIRST when you are about to build a model and do not yet know the
    vocabulary. The `type` strings it returns are exactly what `add_block`
    expects. Plugin block types appear here automatically.

    Returns {"block_types": [{"type", "summary", "required", "multi_output"}]}.
    `multi_output` marks the only types allowed more than one outgoing link
    (DECIDE, LOGIC): every other block may have at most one.
    """
    out = []
    for name in sorted(BLOCK_REGISTRY):
        spec = BLOCK_REGISTRY[name]
        doc = " ".join((spec.cls.__doc__ or "").split())
        out.append({"type": name, "summary": doc.split(". ")[0][:200],
                    "required": [p.name for p in spec.params if p.required],
                    "multi_output": spec.multi_output})
    return {"block_types": out}


@tool(read_only=True)
def describe_block_type(type_name: str) -> dict:
    """Describe one block type in full: what it does and every parameter with
    its type, default and meaning.

    Use this before `add_block`/`update_block` whenever you are unsure which
    params a type takes -- passing an unknown key is an error, and the defaults
    tell you what you may safely leave out.

    Returns {"type", "description", "multi_output", "params": [{"name", "type",
    "default", "required", "description"}]}.
    """
    spec = BLOCK_REGISTRY.get(str(type_name).upper())
    if spec is None:
        raise ToolError("E_UNKNOWN_BLOCK_TYPE", f"unknown block type: {type_name}",
                        f"use one of: {', '.join(sorted(BLOCK_REGISTRY))}")
    return {
        "type": spec.type_name,
        "description": " ".join((spec.cls.__doc__ or "").split()),
        "multi_output": spec.multi_output,
        "params": [{"name": p.name, "type": p.type.__name__, "default": p.default,
                    "required": p.required, "description": p.description} for p in spec.params],
    }


_document_args("list_distributions",
               block_type="Optional block type (e.g. \"CREATE\", \"DELAY\") to list only the "
                          "distributions offered for it; omit for the whole catalogue.")


@tool(read_only=True)
def list_distributions(block_type: str | None = None) -> dict:
    """List the distribution names available for a duration or a random draw.

    Pass `block_type` (CREATE, DELAY, PROCESS, ...) to get only the ones that
    block is usually given; omit it for everything, including plugin entries.
    The names are what goes in a block's `distribution` param, a link delay, or
    a failure's uptime/downtime.

    Returns {"distributions": [{"name", "category", "arrival", "delay",
    "params"}]}; call describe_distribution for parameter meanings.
    """
    names = get_available_distributions(block_type) if block_type else sorted(DISTRIBUTION_REGISTRY)
    out = []
    for name in names:
        spec = DISTRIBUTION_REGISTRY.get(name)
        if spec is None:
            continue
        out.append({"name": name, "category": spec.category, "arrival": spec.arrival,
                    "delay": spec.delay, "params": [p.name for p in spec.params]})
    return {"distributions": out}


@tool(read_only=True)
def describe_distribution(name: str) -> dict:
    """Describe one distribution: when to use it and what each parameter means.

    Use it when choosing between, say, Exponential and Lognormal for a service
    time, or when you need the exact parameter names for `dist_params`. Every
    numeric parameter also accepts an expression string, so a rate may depend
    on an entity attribute.

    Returns {"name", "category", "use_case", "support_lower", "params":
    [{"name", "default", "hint", "kind"}]}.
    """
    spec = DISTRIBUTION_REGISTRY.get(name)
    if spec is None:
        raise ToolError("E_UNKNOWN_DISTRIBUTION", f"unknown distribution: {name}",
                        f"use one of: {', '.join(sorted(DISTRIBUTION_REGISTRY))}")
    return {
        "name": spec.name, "category": spec.category,
        "use_case": " ".join(spec.use_case.split()),
        "arrival": spec.arrival, "delay": spec.delay, "support_lower": spec.support_lower,
        "params": [{"name": p.name, "default": p.default, "hint": p.hint, "kind": p.kind}
                   for p in spec.params],
    }


@tool(read_only=True)
def get_expression_help() -> dict:
    """Explain the expression language used by ASSIGN, DECIDE/HOLD conditions,
    RECORD, ranking expressions and expression-valued distribution parameters.

    Read this before writing any expression: it is a restricted-Python subset
    (an AST allow-list, never eval), so anything not listed here is refused at
    validation time rather than at run time.

    Returns {"names", "operators", "functions": [{"name", "arity"}],
    "reserved", "examples"}.
    """
    functions = []
    for fname in sorted(FUNCTIONS):
        _, _, low, high = FUNCTIONS[fname]
        functions.append({"name": fname,
                          "arity": str(low) if high == low else f"{low}..{'n' if high is None else high}"})
    return {
        "names": ["entity.<attribute>", "now", "TNOW", "true", "false",
                  "any declared model variable", "a submodel parameter inside a definition",
                  "a param that is NOT an expression (a record name, an integer, a choice) takes "
                  "a submodel parameter as {\"$arg\": \"<parameter>\"} instead"],
        "operators": "+ - * / // % **, unary + - not, and/or, comparisons, in/not in against a "
                     "tuple or list literal, a if c else b, tuple and list literals",
        "functions": functions,
        "reserved": sorted(RESERVED_NAMES),
        "examples": ["entity.priority * 2", "NQ('Server') > 5", "EXPO(1.5)",
                     "DISC(0.3, 1, 0.7, 2)", "now - entity.start_time"],
    }


@tool(read_only=True)
def validate_model(model: str) -> dict:
    """Validate a model and return every problem with a stable code, a
    JSON-pointer path, and a hint saying how to fix it.

    Call this after each batch of edits and ALWAYS before running. Errors stop a
    run; warnings (code prefix W_) do not, but usually point at a modelling
    mistake. `requires_run_length` tells you whether the model can drain on its
    own -- if it is true and the run length is 0, a run will be refused.

    Returns {"model", "ok", "errors", "warnings", "requires_run_length",
    "problems": [{"code", "severity", "message", "path", "block", "hint"}]}.
    """
    doc = workspace().document(model)
    layout = doc.to_layout()
    problems = schema_mod.validate_model_structured(layout)
    errors = [p for p in problems if p.severity == "error"]
    return {"model": model, "ok": not errors, "errors": len(errors),
            "warnings": len(problems) - len(errors),
            "requires_run_length": schema_mod.requires_run_length(layout),
            "truncated": max(0, len(problems) - MAX_PROBLEMS),
            "problems": _problem_dicts(problems)}
