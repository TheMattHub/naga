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

Two arms get the same request and the same number of model replies:

- **Naga tools.** The model adds blocks and records, validates, runs and reads the results
  through the tools, and says the answer. An edit answers with the problems it introduced, each
  with a code and a hint, and the model may correct itself. A model's own Python never runs.
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
     files. Nothing in it is typed, and a comparison appears only once every task has at least
     three attempts in both arms: a pilot is not shown. -->

--8<-- "benchmarks/llm_study/results.md"

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

`--tasks` limits a run to some tasks and `--arms` to one arm. The Claude Code run takes
`--max-turns`, and starts the agent with only the permissions the arm needs: the tool server's
tools for one, reading and writing in its folder and running its own script for the other.
