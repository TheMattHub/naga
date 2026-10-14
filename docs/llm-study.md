# Tools or code: an LLM case study

The [agent tools](mcp.md) rest on a claim: a language model that builds a simulation through
them gets a model that is valid, runs and gives the right answer more often than the same model
writing SimPy code for the same request. This page is where that claim is checked. It is a case
study on three models from two vendors and twelve tasks, not a ranking of models and not
evidence about tasks unlike these. Whatever comes out is reported, including a result that goes
against the tools.

## What is measured

Each task is a request as a practitioner would type it, with no block name and no API in it, and
with an answer known from theory: queues, a job shop with rework, priorities, batching, a
resource on a rhythm, a loss system, one stock-and-flow model, one population of agents and one
tank with rates. A task's file under `benchmarks/llm_study/tasks/` holds the request, the
derivation of the answer and of its tolerance, and a reference solution for each arm; the tests
run both and check that they land inside the tolerance.

The arms get the same request and the same number of model replies:

- **Naga tools, block by block.** The first round of the study: the model adds blocks and
  records one call at a time, validates, runs and reads the results through the tools, and says
  the answer. An edit answers with the problems it introduced, each with a code and a hint, and
  the model may correct itself. A model's own Python never runs. These attempts are kept as
  they were made; the tools have changed since, so this arm is not run again.
- **Naga tools, whole model.** The tools as reshaped after that round: `get_model_format`
  describes the model file once, `write_model` takes the whole file in one call and answers with
  its problems, then the model runs it. The local loop offers the compact tool list, Claude Code
  `naga mcp --tools compact`.
- **SimPy code.** The model writes a Python script that uses SimPy. The script is run, and when
  it fails the model is given what it printed and may send a corrected one.

Three outcomes are recorded per attempt, each implying the one before:

| Outcome | Naga tools | SimPy code |
|---|---|---|
| Valid | the model file passes validation and is not empty | the script parses and imports SimPy |
| Runnable | it runs to the end within the time limit | it exits without an error within the time limit |
| Correct | the answer given is within the tolerance, and the model was run through the tools | the answer printed is within the tolerance |

A statistical tolerance is five standard deviations of the SimPy reference's own estimate over
twenty seeds, at the run length the request asks for; a task with nothing random in it has a
tolerance derived from its edge effects or its integration step.

## Two comparisons that do not compare with each other

- **Local models** run inside the engine's own tool loop (what `naga assist` runs), served by
  Ollama on a laptop, with a compact set of tools. This measures a model in one loop.
- **Claude Code** runs headless in an empty folder: for the tools arm with the `naga mcp` server
  and no other tool, for the code arm with a shell limited to running its script. This measures
  a whole coding agent, with its own planning and its own retries, so its numbers say nothing
  about how it ranks against the local models.

## Results

<!-- LLM STUDY: benchmarks/llm_study/results.md is written by `study.py report` from the attempt
     files. Nothing in it is typed. An arm of a model appears once every task has at least one
     attempt in it, and a model once its code arm and a tools arm have; attempts in an arm not
     shown are a pilot, counted and not tabulated. -->

--8<-- "benchmarks/llm_study/results.md"

### Why the larger local model was stopped short

Three attempts per task and arm were planned for each local model. The smaller model's run was
completed. The larger model's run was stopped once it had attempted every task in both arms,
about three fifths of the way through, because it was already showing the smaller model's
pattern: its tools arm almost never ended with a correct model while its code arm was right
more often than not, and the remaining rounds, at several minutes per attempt on a laptop,
could not have reversed that. Its tables count only the attempts that were made.

### What came out

The study ran twice. The first time the tools built a model block by block, and the claim did
not hold: with both local models and with Claude Code, writing SimPy code gave a correct answer
more often, in fewer turns and for less. The small local models rarely finished a model at all:
they placed blocks with wrong parameters, left them unconnected and ran out of replies. The cost
came from the number of round trips, each of which reads the whole conversation again, not from
what the model wrote.

So the tools changed shape. A model now writes the whole model file in one call, with
`write_model`, after one call to `get_model_format` that describes the file; the answer is "ok"
or the problems with their places in the file, and a run and a table of results follow. The
second run, the rows "whole model" above, measures that: Claude Code built a correct model
through the tools on more tasks than it wrote correct SimPy, in a few calls, reading far less
than before though still more than the code arm, and the smaller local model, which had built no
correct model block by block, now built some. Three of the whole-model Claude attempts (the
first round of `tank` and `carpark`) were made again after fixes that round found; the earlier
attempts are not counted.

What the tools give that the code does not, a model that is validated, can be opened in an
editor, diffed and run again by someone else, or followed as a twin, is not measured here. The
fixes the two runs found (an argument sent as text, a required argument put inside another, a
distribution's unknown parameter, a condition reading a capacity, a timer profile's period) are
in the changelog.

## What this does not show

- Twelve tasks with textbook answers are not a practitioner's work. A request with a known
  answer is also one a model may have seen solved.
- A script can print a formula's value without simulating anything, and so can a reply. The
  tools arm must at least have run its model; the code arm must at least import SimPy. Neither
  check proves the number came from the run.
- The tools arm is given a system prompt that describes the workflow, the code arm a short one:
  each arm gets the help its interface comes with, which is part of what is compared.
- A local model's result depends on the server's settings, the context length above all: a
  conversation that no longer fits is cut silently.

## Run it again

Every attempt is one file under `benchmarks/llm_study/attempts/`, written when it ends. A run
can be stopped at any time; started again, it skips what is on file.

<!-- no-run: needs a local model server and takes hours -->
```console
$ python benchmarks/llm_study/study.py local --models gemma4:12b --repeats 3
```

<!-- no-run: needs Claude Code logged in, and uses its subscription's quota -->
```console
$ python benchmarks/llm_study/study.py claude --model haiku --repeats 3
```

<!-- no-run: rewrites the results files of the checkout it is run in -->
```console
$ python benchmarks/llm_study/study.py report
```

`--tasks` limits a run to some tasks and `--arms` to one arm (`whole` or `simpy`; the block-by-block arm is a record and is not run again). The Claude Code run takes
`--max-turns`, and starts the agent with only the permissions the arm needs: the tool server's
tools for one, reading and writing in its folder and running its own script for the other.
