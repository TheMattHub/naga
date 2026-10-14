### The tasks

| Task | What is asked | Answer | Accepted within |
|---|---|---|---|
| `mm1` | One clerk, one queue (M/M/1): the mean time a customer spends in the shop | 12 | 2.1 |
| `mmc` | Three agents, one queue (M/M/3): the mean wait before a call is answered | 3.407 | 0.77 |
| `md1` | A machine with a fixed cycle time (M/D/1): the mean wait before a job starts | 2.25 | 0.3 |
| `tandem` | Two machines in a row (tandem M/M/1 queues): the mean time a part spends in the line | 19.17 | 2.5 |
| `rework` | Machining and inspection with rework (a Jackson network): the mean time a job is in the shop | 26.67 | 3.1 |
| `priority` | Urgent jobs go first (a non-preemptive priority M/M/1 queue): the mean time of an urgent job | 7.5 | 0.53 |
| `batch` | Trays of five through a tunnel oven (batching): the mean time of an item | 7 | 0.12 |
| `carpark` | A car park with ten spaces and no queue (an Erlang loss system): the share turned away | 9.954 | 0.98 |
| `shift` | A robot that works 65 minutes and charges 35 (a capacity schedule): the mean time of a parcel | 13 | 0.25 |
| `cooling` | A cup of coffee cooling (system dynamics, one stock): its temperature after 15 minutes | 35.62 | 0.5 |
| `repair` | Twenty machines that fail and are repaired (agents with two states): how many run on average | 18.18 | 0.27 |
| `tank` | A tank filled by a pump on a timer and drawn from steadily (rates): the share of time it is empty | 16.67 | 0.5 |

### Local models, in the engine's own tool loop

Attempts made from 2026-10-13 to 2026-10-14: 12 tasks, 1 to 3 attempts per task and arm, at most 25 model replies per attempt. Naga 0.6.0, SimPy 4.1.1, Python 3.12.4.

`gemma4:12b` was stopped after 42 of the 72 planned attempts (3 per task and arm); its cells count what was made.

`hf.co/unsloth/gemma-4-E4B-it-GGUF:Q4_K_M` was stopped after 84 of the 108 planned attempts (3 per task and arm); its cells count what was made.

| Model | Arm | Attempts | Valid | Runnable | Correct | Median replies | Median tool calls | Median input tokens (k) | Median output tokens (k) | Median cost (USD) | Median minutes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `gemma4:12b` | Naga tools, block by block | 21 | 3/21 (14%) | 3/21 (14%) | 1/21 (5%) | 25 | 24 | 172 | 0.579 |  | 3.68 |
| `gemma4:12b` | SimPy code | 21 | 20/21 (95%) | 20/21 (95%) | 13/21 (62%) | 1 | 1 | 0.223 | 1.31 |  | 7.08 |
| `hf.co/unsloth/gemma-4-E4B-it-GGUF:Q4_K_M` | Naga tools, block by block | 36 | 1/36 (3%) | 0/36 (0%) | 0/36 (0%) | 25 | 24 | 234 | 2.27 |  | 6.91 |
| `hf.co/unsloth/gemma-4-E4B-it-GGUF:Q4_K_M` | Naga tools, whole model | 12 | 7/12 (58%) | 6/12 (50%) | 3/12 (25%) | 10 | 9 | 93.9 | 4.07 |  | 12.1 |
| `hf.co/unsloth/gemma-4-E4B-it-GGUF:Q4_K_M` | SimPy code | 36 | 35/36 (97%) | 34/36 (94%) | 18/36 (50%) | 1 | 1 | 0.23 | 1.76 |  | 3.86 |

Correct attempts per task:

| Task | `gemma4:12b`, Naga tools, block by block | `gemma4:12b`, SimPy code | `hf.co/unsloth/gemma-4-E4B-it-GGUF:Q4_K_M`, Naga tools, block by block | `hf.co/unsloth/gemma-4-E4B-it-GGUF:Q4_K_M`, Naga tools, whole model | `hf.co/unsloth/gemma-4-E4B-it-GGUF:Q4_K_M`, SimPy code |
|---|---|---|---|---|---|
| `mm1`: One clerk, one queue (M/M/1): the mean time a customer spends in the shop | 0/2 | 2/2 | 0/3 | 1/1 | 3/3 |
| `mmc`: Three agents, one queue (M/M/3): the mean wait before a call is answered | 0/2 | 0/2 | 0/3 | 0/1 | 2/3 |
| `md1`: A machine with a fixed cycle time (M/D/1): the mean wait before a job starts | 0/2 | 1/2 | 0/3 | 1/1 | 1/3 |
| `tandem`: Two machines in a row (tandem M/M/1 queues): the mean time a part spends in the line | 1/2 | 2/2 | 0/3 | 0/1 | 2/3 |
| `rework`: Machining and inspection with rework (a Jackson network): the mean time a job is in the shop | 0/2 | 2/2 | 0/3 | 0/1 | 2/3 |
| `priority`: Urgent jobs go first (a non-preemptive priority M/M/1 queue): the mean time of an urgent job | 0/2 | 2/2 | 0/3 | 0/1 | 0/3 |
| `batch`: Trays of five through a tunnel oven (batching): the mean time of an item | 0/2 | 1/2 | 0/3 | 0/1 | 1/3 |
| `carpark`: A car park with ten spaces and no queue (an Erlang loss system): the share turned away | 0/2 | 1/2 | 0/3 | 0/1 | 1/3 |
| `shift`: A robot that works 65 minutes and charges 35 (a capacity schedule): the mean time of a parcel | 0/2 | 0/2 | 0/3 | 0/1 | 1/3 |
| `cooling`: A cup of coffee cooling (system dynamics, one stock): its temperature after 15 minutes | 0/1 | 1/1 | 0/3 | 1/1 | 1/3 |
| `repair`: Twenty machines that fail and are repaired (agents with two states): how many run on average | 0/1 | 1/1 | 0/3 | 0/1 | 2/3 |
| `tank`: A tank filled by a pump on a timer and drawn from steadily (rates): the share of time it is empty | 0/1 | 0/1 | 0/3 | 0/1 | 2/3 |

### Claude Code, the whole coding agent

Attempts made from 2026-10-14 to 2026-10-14: 12 tasks, 3 attempts per task and arm, at most 50 agent turns per attempt. Naga 0.6.1, SimPy 4.1.1, Python 3.12.4. Model ids as reported by Claude Code: claude-haiku-4-5-20251001.

| Model | Arm | Attempts | Valid | Runnable | Correct | Median replies | Median tool calls | Median input tokens (k) | Median output tokens (k) | Median cost (USD) | Median minutes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `haiku` | Naga tools, block by block | 36 | 35/36 (97%) | 35/36 (97%) | 27/36 (75%) | 17.5 | 16.5 | 429 | 3.52 | 0.098 | 0.957 |
| `haiku` | Naga tools, whole model | 36 | 36/36 (100%) | 36/36 (100%) | 35/36 (97%) | 6 | 5 | 83 | 4.49 | 0.0582 | 0.8 |
| `haiku` | SimPy code | 36 | 36/36 (100%) | 35/36 (97%) | 32/36 (89%) | 3 | 2 | 31.4 | 3.92 | 0.0358 | 0.562 |

Correct attempts per task:

| Task | `haiku`, Naga tools, block by block | `haiku`, Naga tools, whole model | `haiku`, SimPy code |
|---|---|---|---|
| `mm1`: One clerk, one queue (M/M/1): the mean time a customer spends in the shop | 3/3 | 3/3 | 3/3 |
| `mmc`: Three agents, one queue (M/M/3): the mean wait before a call is answered | 2/3 | 3/3 | 3/3 |
| `md1`: A machine with a fixed cycle time (M/D/1): the mean wait before a job starts | 3/3 | 3/3 | 3/3 |
| `tandem`: Two machines in a row (tandem M/M/1 queues): the mean time a part spends in the line | 3/3 | 3/3 | 3/3 |
| `rework`: Machining and inspection with rework (a Jackson network): the mean time a job is in the shop | 3/3 | 3/3 | 2/3 |
| `priority`: Urgent jobs go first (a non-preemptive priority M/M/1 queue): the mean time of an urgent job | 1/3 | 3/3 | 2/3 |
| `batch`: Trays of five through a tunnel oven (batching): the mean time of an item | 2/3 | 3/3 | 3/3 |
| `carpark`: A car park with ten spaces and no queue (an Erlang loss system): the share turned away | 1/3 | 3/3 | 3/3 |
| `shift`: A robot that works 65 minutes and charges 35 (a capacity schedule): the mean time of a parcel | 3/3 | 3/3 | 3/3 |
| `cooling`: A cup of coffee cooling (system dynamics, one stock): its temperature after 15 minutes | 3/3 | 3/3 | 3/3 |
| `repair`: Twenty machines that fail and are repaired (agents with two states): how many run on average | 2/3 | 3/3 | 3/3 |
| `tank`: A tank filled by a pump on a timer and drawn from steadily (rates): the share of time it is empty | 1/3 | 2/3 | 1/3 |
