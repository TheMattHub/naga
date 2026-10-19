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
     `study_add_input(text=...)` or `study_add_input(path=...)`, extract the decision, goals and constraints,
     and ask only about the gaps;
   - the **interview**: rounds of numbered questions with your recommended answer each
     (`references/interview.md`).
3. State the effort before building (`study_status` gives the estimate for the size), and the two modes:
   - **one go** (default for a first study): show **one plan card** (goals with their acceptance ranges,
     scope, the assumptions you will make, effort, mode); after the person approves it, record
     `plan_approved` and build to the target without further questions, recording every answer you chose
     as an assumption they can revisit;
   - **incremental**: show each step and let the person steer.
4. Record what you learn with `study_update(changes={...})`: goals as KPI ranges, the target components,
   assumptions with their source, sources with their input file.
5. Build by sending the **whole model** to `study_step` (call `get_model_format` once first). A step that
   does not run is kept as a draft with its problems: fix and step again. Look at `study_view` when the
   structure matters; it shows what changed and what is still missing.
6. Validate against the person's data, a known answer (a simplified case with a textbook result) or an
   expert's judgement, and record it with its level.
7. Run the scenarios the decision needs, then give the answer with the control room (`study_show`) and,
   when the person is satisfied, the package (`study_package`).

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

## Out of scope

Say so plainly when simulation cannot answer the question (a pure accounting sum, a one-off physical
calculation, a question without a process), and suggest what can.

## More

- `references/interview.md`: how to interview when the person prefers questions.
- `references/method.md`: the study steps from the simulation literature, for when you need the why.
