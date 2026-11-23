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
   carries a `next_step` with a ready-to-send call: do that, or better when you know better. The folder is
   the memory, so a new chat resumes from it. `study_start(..., changes={...})` records the whole plan card
   in the same call. It also carries `options`: what next, ranked from the study's numbers. When the person
   asks "what next?", or nothing is pending, offer the first as one question with its reason and name up to
   two others; run one only after the person picks it.
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
   `get_model_format` once first), or the name of a model open in the workspace (`model=`), or a
   `patch=` on the last version; `changes={...}` in the same call
   records what you learned (for record-only changes use `study_update`). A step that
   does not run is kept as a draft with its problems: fix and step again. The engine picks the
   replications from the study's precision target and says how many and how precise; when the cap stops
   it first, relax the target and record that as an assumption, or move to the deep size. `study_view` shows
   the flowchart: what changed and what is still missing.
6. Validate: only the engine or the person can make "The model matches reality" pass. For a single
   queue, `study_validate(study=...)` runs the textbook comparison and records it as checked. Otherwise
   show the person what to compare (which KPI, against which data) and ask them to record their own
   check: **I checked this** in the live room, or `naga study confirm FOLDER --text "..." --against "..."`.
   No tool can record it for them; your own record reads "reported, not checked". When the status says
   the person's check was on an earlier version (the blocks or links changed), ask them to check again.
7. Run the scenarios the decision needs, then give the answer with the control room (`study_show`) and,
   when the person is satisfied, the package (`study_package`).

## Ask the person first

Before building, ask what only the person can decide (the decision and its options, what counts as
enough, the scope), each with your recommended answer; record what you choose as an assumption.

## Records (`study_update(study, changes={...})`)

A list key replaces the whole list, so send every entry each time. Near-miss shapes are accepted and
answered under `normalised` with the canonical form; use that form next time.

- goal: `{"id": "g1", "text": "orders wait under 15 min", "kpi": "Packer/Time in Queue Avg", "high": 15}`
  (`low` and/or `high`; a goal without them stays text and is never met);
- tail goal: `{"id": "g2", "text": "at most 5% wait over 15 min", "kpi": "Packer/Time in Queue",
  "share_above": 15, "high": 0.05}` (KPI `Packer/Share waiting over 15`); `"kpi": "Total Wait"`
  sums all queues;
- `"precision": {"relative": 0.05}` to change the study's target (half-width at 95%);
- `"target"`: a list of components, `[{"name": "Packing", "purpose": "...", "blocks": ["pack"]}]`
  (blocks once built);
- assumption: `{"id": "a1", "text": "arrivals are random", "value": "exponential, mean 6 min", "source": "manager"}`;
- source: `{"id": "s1", "text": "manager's message", "input": "inputs/message.txt"}`;
- validation: `{"level": "data", "passed": true, "text": "wait vs log"}` (`level`: `data`,
  `known_answer` or `expert`); yours reads "reported, not checked", never passing;
- experiment: `{"id": "e1", "text": "one vs two stations", "result": "..."}`;
- `"plan_approved": true` once the person approves the plan card.

KPI names: a system KPI as the run lists it (`Avg Throughput Time`), a resource column as
`"<resource>/<column>"` (`Packer/Time in Queue Avg` is the wait before a resource), or a statistic you
record in the model. `study_step` answers the KPIs it measured; use those names.

## Scenarios

Put the alternatives in the model file, the base model being the baseline:
`"scenarios": [{"name": "Two stations", "overrides": [{"path": "resources.Packer.capacity", "value": 2}]}]`.
Every `study_step` runs them with the same replications and seed and answers, per scenario, each goal
KPI (mean, confidence half-width, paired difference) and whether each goal is met.
The size sets the replications and caps the scenarios; call one option better only when its paired
difference's interval leaves out zero. When the base model misses a goal and a
scenario meets them all, the label says so ("sufficient for its goals with: Two stations"): the answer
to the person is that option.

## Reading the results

Each goal KPI of a scenario carries "diff_vs_baseline", the paired difference from the baseline on
common random numbers: `{"mean", "lo", "hi", "n"}`, a 95% interval over n pairs. Say it as the pages do:
"a difference of -3.4 (95% interval -3.8 to -2.9)" when the interval leaves out zero, "no clear
difference" when it holds zero (never "slightly better"). A share KPI (a tail goal's "Share ...") is a
fraction: give its difference in percentage points. The pages add one line when an option made a KPI
markedly steadier; quote it if asked, never infer it from "spread", and never call it a cause or put it
in the headline. With an option recommended, the room's goal chips and the client page's goals table
lead with it, the baseline beside it.

## Modelling notes

- A shift that starts empty and ends is a finite-horizon study: report the shift's averages and say so.
  A steady-state question (long-run behaviour) needs a long run with a warm-up; the two can differ a lot
  near full load, so pick the one the decision is about and name it.
- "How many wait longer than X" is a tail goal (above); averages hide the tail.
- A known-answer check: a simplified case with a textbook result is a good validation at the
  `known_answer` level; `study_validate` does it for a single queue.

## Tools

`study_start`, `study_status`, `study_update`, `study_add_input`, `study_step`, `study_validate`,
`study_view`, `study_show`, `study_revert`, `study_accept` (the person's only), and on request
`study_report` (a version's full report), `study_package` (the deliverable); plus `get_model_format` for the
model file's format.
When no tool is obviously right, `find_tool` names candidates. If its answer carries `guidance`, try its
first three in order: when a call fails because it was the wrong tool, call the next one, without
explaining the miss, and stop after three.

## Output

Each version makes the light output by default (the step's summary, the flowchart, the control room).
Every study answer carries the page link, and the live room link while a room runs: give the person the room link
once, early. The room updates itself and has buttons, so the person may steer from it; a message that
starts "Not quite" or "Carry on with the study" came from those buttons and is the person's request.
Make the heavy outputs only when the person asks: `study_report`, `study_view`
(PNG), `study_animation` (GIF), `study_sensitivity` (which assumptions change the answer: suggest it
before a decision rests on assumptions), `study_extensions` (verified candidate modules to offer the
person), `study_writeup` (a paper-style report; pass your own `discussion` text), `study_slides` (a deck)
and `study_package`.

## Principles

- **The simplest model that answers the question.** Add detail only when a goal or a check needs it.
- **Never claim more than the verdicts.** `not_verified` is not passed. Say "sufficient for its goals" or
  "not yet sufficient", never "finished". Name the validation level with every result.
- **Facts are yours to find, decisions are the person's.** Look up what documents, data or the engine can
  settle; ask the person only what they must decide.
- **Only the person accepts.** Never call `study_accept` on your own judgement; they give the reason.
- **Only the engine or the person validates.** Your validation note is "reported, not checked". When a
  page or answer offers a re-check, offer it: `study_validate` where it applies, else ask the person.
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
