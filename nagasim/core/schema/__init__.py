"""Model schema: versioning, migration and validation (ARCHITECTURE section 7.5).

A package since ARCH-REVIEW C2; this module re-exports every name callers
import, so `from nagasim.core.schema import X` and `schema.X` keep working:

- `migrate`: `CURRENT_VERSION`, `migrate`, `ModelValidationError`, `CONSENT_KEYS`
- `prepare`: `read_layout`, `load_layout`, `prepare`, `validate_model[_structured]`,
  `fatal_problems`, `format_problems`, `requires_run_length`
- `layout`: `validate_layout_structured` (runs every section validator)
- `graph`: `validate_graph_structured`
- `submodels`: `validate_submodels_structured`
- `sections_validation`: scenarios, reality checks, assertions, dashboard, data
  sources/bindings and travel blocks
- `common`: helpers the validators share (`_check_expression`, ...)

Resources and worker fleets are validated by their owners
(`resource_manager.validate_resources_structured`,
`movers.validate_workers_structured`).
"""
from __future__ import annotations

from nagasim.core.problems import PROBLEM_CODES, Problem, problem
from nagasim.core.schema.migrate import (CONSENT_KEYS, CURRENT_VERSION, ModelValidationError,
                                         migrate)
from nagasim.core.schema.sections_validation import (CONNECTION_SOURCE_KINDS, DATA_SOURCE_KINDS,
                                                     DATA_TRANSFORM_PREFIXES, DATA_TRANSFORMS,
                                                     LIVE_SOURCE_OPTIONS, REMOTE_SOURCE_KINDS,
                                                     consent_problems, password_in_url)
from nagasim.core.schema.layout import _validate_background, validate_layout_structured
from nagasim.core.schema.graph import validate_graph_structured
from nagasim.core.schema.submodels import validate_submodels_structured
from nagasim.core.schema.prepare import (fatal_problems, format_problems, load_layout, prepare,
                                         read_layout, requires_run_length, validate_model,
                                         validate_model_structured)
from nagasim.core.seizing import QUEUEING_BLOCK_TYPES

__all__ = ["CURRENT_VERSION", "ModelValidationError", "migrate", "validate_model",
           "requires_run_length", "Problem", "PROBLEM_CODES", "problem",
           "validate_layout_structured", "validate_graph_structured",
           "validate_submodels_structured", "validate_model_structured", "fatal_problems",
           "format_problems", "prepare", "read_layout", "load_layout",
           "DATA_SOURCE_KINDS", "REMOTE_SOURCE_KINDS", "CONNECTION_SOURCE_KINDS",
           "LIVE_SOURCE_OPTIONS", "DATA_TRANSFORMS",
           "DATA_TRANSFORM_PREFIXES", "password_in_url", "CONSENT_KEYS", "consent_problems",
           "QUEUEING_BLOCK_TYPES"]
