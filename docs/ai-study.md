# Run a study with an assistant

The engine can keep a whole simulation study in a folder and test every model step, so any
assistant that speaks to it (see [Use with an agent](mcp.md)) can run a study without remembering
anything between calls. The folder is the memory: each call names it, reads it and writes it back.
That makes a study resumable by a different assistant, or by you, the next day.

## What a study folder holds

A study folder is an ordinary folder under the workspace root, and its id is its path relative to
that root. It holds the question and the decision behind it, the goals with the KPI ranges that
would meet them, the assumptions and their sources, the plan and whether the person approved it,
the validation done, and every model version with its checks and report. Inputs the person
supplies (a spreadsheet, a transcript) are kept under `inputs/`.

## The three calls

Three calls are enough to run a study; an assistant makes them in this order and repeats the last two.

- `study_start` takes the decision question, makes the folder and answers with a plan card and the next step.
- `study_status` shows where the study stands on one screen: the goals, the checklist, the last version's KPIs and the next step.
- `study_step` takes the complete model file and a one-line note, runs and checks it, and stores it as a new version.

A step whose model does not validate or run is stored as a draft with its problems, not refused,
so the assistant reads them, fixes the model and steps again. Every answer ends with a `next_step`,
which is all the assistant needs to follow. The other tools keep the record:

- `study_update` records what was learned or decided: goals, assumptions, sources, validation, the approved plan.
- `study_add_input` keeps a source file or a piece of text under `inputs/`.
- `study_revert` goes back to an earlier version, recorded as a new version so no history is lost.
- `study_accept` records that the person accepts the study as it stands.

## Seeing the model

Every version keeps a flowchart of its model, so the person and the assistant can see what each step
built. Blocks added or changed in that step have an orange outline, and parts of the agreed target that
have no blocks yet are drawn dashed to the right. The flowchart is stored beside the version as
`flowchart.svg` and as `flowchart.txt`, a plain list of the blocks and links. `study_view` returns it
to the assistant as a PNG image over MCP (with `nagasim[plot]` installed), as SVG, or as that text,
which is what an assistant without image support reads. Looking at the drawing is often the quickest
way to catch a wrong link.

## How many replications

The engine chooses the number of replications from the study's precision target, so the count is a
stated decision rather than a guess. Every goal KPI should reach a 95% confidence half-width of 10% of
its mean in a quick study, 5% in a standard one and 2% in a deep one (or a target the person sets). A step
runs 10 replications, works out from their spread how many each goal KPI needs, and reruns with the
largest count and the same seed, up to 50, 200 or 1000. When the cap stops it first, the step says so and
the precision check is partial, never passed. Each step's answer, the control room and the report state
the count, the target and the precision reached.

## What each version makes, and what is on request

Each version makes only what the person needs to follow the study: a short summary of what changed,
the goal KPIs with their intervals, the flowchart and the refreshed control room. Heavier output is made
when someone asks for it, from the stored versions: `study_report` for a version's full run report,
`study_view` for a PNG of the flowchart and `study_package` for the deliverable. This keeps each step
quick and the conversation short.

## The control room and the package

Each study has one page, `control-room.html` in its folder, that shows where the study stands on one
screen. The header carries the verdict, one chip per goal with its value, interval and trend, the
precision and the item still open. Below it, panels open on demand: goals and checklist, the latest
model's flowchart, the versions (one row each, expanding to their changes and flowchart), the scenarios
against the goals, the assumptions and sources, and what can be made on request. The page runs no script,
so it can be emailed or put on a shared drive, and printing it gives the PDF with every panel open. When
the person is satisfied, `study_package` writes the deliverable to `package/`: the page, an Excel workbook,
the current model and an RO-Crate description. The page embeds the study's record, so a copy of it is
enough to pick the study up again.

## Fewer, safer tool calls

The study tools accept the common near-misses an assistant makes and say what they changed. A goal's
range given as `range: [0, 5]`, a record field called `statement` instead of `text` or a single record
where a list is expected is stored in the canonical form, and the answer lists each change under
`normalised`; ambiguous input is refused with a corrected call built from the caller's own values. An
unknown tool or argument name answers "did you mean". `study_start` takes the whole plan in one call and
`study_step` can record what was learned in the same call as the step, so a study needs fewer calls.
`naga study rerun FOLDER` reruns every version from its stored model and seed and reports whether the
results reproduce, so a study can be checked without an assistant.

## In the chat

In assistants that support MCP Apps, the control room opens inside the chat when the assistant calls
`study_show`. The page there is the same as the file, with two additions the person can use directly: a
button beside each earlier version to go back to it, and a box to accept the study as it stands with a
reason in their own words. The buttons call the study tools through the assistant's app, which may ask
the person to confirm; nothing is accepted without the typed reason. Assistants without MCP Apps get a
short text summary and the path of the page file, so the study works the same way everywhere.

## The study guide

The engine ships a short study guide for assistants, so the method travels with the tools. It is an
Agent Skills folder, the format most assistants read: the protocol in a page, an interview the assistant
runs in rounds of numbered questions with a recommended answer each, and the study steps from the
simulation literature with their sources. `naga skill` prints where it is and `naga skill --copy DIR`
copies it into an assistant's skills folder; over MCP the same text is the `run_simulation_study` and
`study_interview` prompts, and every study folder's `AGENTS.md` points to it. The guide states goals and
principles only, because the engine's checks are what keep a study honest.

## Verdicts and validation levels

Every check, and every checklist item, ends in one of four verdicts: `passed`, `partial`,
`failed` or `not_verified`. A `not_verified` item was not tested and never counts as passed,
so a gap shows as a gap. Validation is recorded at one of three levels, from weakest to
strongest: `data` (the inputs match the source data), `known_answer` (the model reproduces a
result known from theory or measurement) and `expert` (someone who knows the system reviewed it).

## Sufficient for its goals, never finished

A study is called "sufficient for its goals" or "not yet sufficient", and never finished, because
a model can always be improved and the engine cannot judge that for the person. The label
follows the checks: the goals have KPI ranges, the validation is recorded and nothing has failed.
Only the person may accept a study as it stands, with a reason in their own words; an assistant
must not call `study_accept` on its own judgment. Accepting changes the label and not the checks,
so open issues stay listed.

## One go or step by step

A study runs in one of two modes. In `one_go` the assistant drafts a plan, the person approves it
once, and the model is then built in a run; in `incremental` the model grows in small steps the
person can follow. The `size` (`quick`, `standard` or `deep`) caps the replications, steps and
scenarios, so the effort stays what the person agreed to.

## A study in Python

The tools are functions, so the same calls run without an assistant. This runs as it stands, in a
temporary workspace.

```python
import json
import tempfile
from pathlib import Path

import nagasim
from nagasim import agent_tools
from nagasim.agent_tools import call

agent_tools.use_workspace(agent_tools.Workspace(tempfile.mkdtemp()))

started = call("study_start", question="Is one server enough?")
study = started["study"]

model = Path(nagasim.__file__).parent / "models" / "single_server.json"
step = call("study_step", study=study, layout=json.loads(model.read_text(encoding="utf-8")),
            note="the single-server queue")
print(step["kind"], step["verdict"])
print(call("study_status", study=study)["sufficiency"])
```
