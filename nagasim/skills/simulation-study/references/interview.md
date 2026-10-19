# The interview

Adapted from the "grilling" interview in mattpocock/skills (MIT License, Copyright (c) 2026 Matt Pocock,
https://github.com/mattpocock/skills), shaped for simulation studies and for people who are not modellers.

## The idea

The study is a tree of decisions: what the question is decides which goals matter, the goals decide the
scope, the scope decides which data and assumptions are needed. Ask in **rounds**. Each round asks every
decision whose prerequisites are already settled, and nothing that depends on an answer still open. A
dozen questions usually take three rounds, not twelve.

## A round

Number each question, give it a short title, one or two sentences of body, and your **recommended
answer** on its own line, so the person can reply "1 yes, 2 the second, 3 no because ...":

1. **The decision.** Is the question whether to buy a second packing machine before the summer peak?
   Recommended: yes, judged on the peak week.
2. **What "enough" means.** Shall we call one machine enough if orders wait under 4 hours on average?
   Recommended: under 4 hours, and no day with a backlog above 200 orders.

Keep it to what the person can answer from their own knowledge. If they cannot answer, offer to assume
your recommendation and record it as an assumption.

## Facts versus decisions

Facts are yours: look them up in the documents they gave you, their data, the engine (`get_model_format`,
block and distribution lists), or a quick calculation. Decisions are theirs: what to decide, what counts
as enough, which options are on the table, which risks matter. Answering their decisions for them breaks
the interview; asking them for facts you could find wastes their time.

## Typical branches

- The decision and its options (what would they do differently depending on the answer?).
- Goals as KPI ranges (waiting time, throughput, utilisation, cost, service level) and the horizon.
- Scope: which part of the process, which products, which shifts; what stays outside.
- Demand and variability: arrival pattern, peaks, mix.
- Resources and rules: capacities, schedules, breakdowns, priorities.
- Data: what exists, how old, who owns it; otherwise an expert estimate recorded as such.

## When to stop

Stop when no open decision remains or the person wants to move on. Then play back a short summary (the
plan card in one-go mode) and wait for their confirmation before building. If the person prefers one
question at a time, do that instead.
