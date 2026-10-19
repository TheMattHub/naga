---
name: simulation-study
description: Run a simulation study with the Naga engine for a person who has a decision to make (how many machines, staff or docks; will a change cope with demand; what a layout or policy costs). Use when someone asks a what-if, capacity or "is this enough" question that a model of a process can answer, or asks to continue a study folder.
---

# Simulation study

A person has a decision and little time. You build and run the model through the engine's study tools;
the engine keeps the record, tests every step and tells you what to do next. Your job is to understand the
decision, ask only what you cannot find out, and report honestly.

## The protocol

1. `study_start(question=...)` once, or `study_status(study=...)` for an existing folder. Every answer
   carries a `next_step`: do that. The folder is the memory, so a new chat resumes from it.
2. Intake, the person's choice:
   - a **free-flow description** (a voice message, a long text, documents): store it with
     `study_add_input(study, name="message.txt", text=...)` or `path=...`, extract the decision, goals and
     constraints, and ask only about the gaps;
   - the **interview**: rounds of numbered questions with your recommended answer each
     (`references/interview.md`).
3. State the effort before building (`study_status` answers `effort`), and the two modes:
   - **one go** (default for a first study): show **one plan card** (goals with their acceptance ranges,
     scope, the assumptions you will make, effort, mode); after the person approves it, record
     `plan_approved` and build to the target without further questions, recording every answer you chose
     as an assumption they can revisit;
   - **incremental**: show each step and let the person steer.
4. Record what you learn with `study_update` (shapes below).
5. Build by sending the **whole model** to `study_step(study, layout={...}, note=...)` (call
   `get_model_format` once first). A step that does not run is kept as a draft with its problems: fix and
   step again. `study_view` shows the flowchart: what changed and what is still missing.
6. Validate against the person's data, a known answer (a simplified case with a textbook result) or an
   expert's judgement, and record it with its level.
7. Run the scenarios the decision needs, then give the answer with the control room (`study_show`) and,
   when the person is satisfied, the package (`study_package`).

## Records (`study_update(study, changes={...})`)

A list key replaces the whole list, so send every entry each time.

- goal: `{"id": "g1", "text": "orders wait under 15 min", "kpi": "Packer/Time in Queue Avg", "high": 15}`
  (`low` and/or `high`; a goal without them stays text and is never met);
- `"target"`: a list of components, `[{"name": "Packing", "purpose": "...", "blocks": ["pack"]}]`
  (blocks once built);
- assumption: `{"id": "a1", "text": "arrivals are random", "value": "exponential, mean 6 min", "source": "manager"}`;
- source: `{"id": "s1", "text": "manager's message", "input": "inputs/message.txt"}`;
- validation: `{"level": "known_answer", "passed": true, "text": "M/G/1 wait vs model"}`
  (`level`: `data`, `known_answer` or `expert`);
- experiment: `{"id": "e1", "text": "one vs two stations", "result": "..."}`;
- `"plan_approved": true` once the person approves the plan card.

KPI names: a system KPI as the run lists it (`Avg Throughput Time`), a resource column as
`"<resource>/<column>"` (`Packer/Time in Queue Avg` is the wait before a resource), or a statistic you
record in the model. `study_step` answers the KPIs it measured; use those names.

## Scenarios

Put the alternatives in the model file, the base model being the baseline:
`"scenarios": [{"name": "Two stations", "overrides": [{"path": "resources.Packer.capacity", "value": 2}]}]`.
Every `study_step` runs them with the same replications and seed and answers, per scenario, each goal
KPI (mean and confidence half-width) and whether each goal is met. Record the comparison with
`study_update(changes={"experiments": [...]})`. The size sets the replications and caps the scenarios;
call one option better only when the intervals separate.

## Modelling notes

- A shift that starts empty and ends is a finite-horizon study: report the shift's averages and say so.
  A steady-state question (long-run behaviour) needs a long run with a warm-up; the two can differ a lot
  near full load, so pick the one the decision is about and name it.
- "How many wait longer than X" needs a statistic the model records (an assignment of the wait and a
  record of whether it exceeded X); averages hide the tail.
- A known-answer check: a simplified scenario with a textbook result (a single queue against the M/M/1 or
  M/G/1 formula, run long) is a good validation at the `known_answer` level.

## Tools

`study_start`, `study_status`, `study_update`, `study_add_input`, `study_step`, `study_view`,
`study_show`, `study_revert`, `study_accept` (the person's only), `study_package`; plus `get_model_format` for the model
file's format.

## Principles

- **The simplest model that answers the question.** Add detail only when a goal or a check needs it.
- **Never claim more than the verdicts.** `not_verified` is not passed. Say "sufficient for its goals" or
  "not yet sufficient", never "finished". Name the validation level with every result.
- **Facts are yours to find, decisions are the person's.** Look up what documents, data or the engine can
  settle; ask the person only what they must decide.
- **Only the person accepts.** Never call `study_accept` on your own judgement; they give the reason.
- **Small, visible steps.** After each version, one or two sentences: what changed, what the numbers say,
  what is next.
- **Respect the size.** A quick question gets a quick study; the checklist records what was skipped.
- For a deep study, suggest a reviewer pass: a fresh assistant reads only the control room and lists its
  doubts before delivery.

Say so plainly when simulation cannot answer the question (a pure accounting sum, a one-off physical
calculation, a question without a process), and suggest what can.

## More

- `references/interview.md`: how to interview when the person prefers questions.
- `references/method.md`: the study steps from the simulation literature, for when you need the why.
