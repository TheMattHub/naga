# Pick tools with a small model (optional)

`find_tool` ranks the agent tools by plain word overlap, and a small re-ranking model can put the
right tool first more often. The tool picker is that model: an optional extra that reorders
`find_tool`'s answer, either a small local model (`gte`) or hosted Claude Haiku (`haiku`). Both are
suggestions the assistant may follow, and both are off by default, so a new install behaves exactly as before.

## Turn it on

The person who starts the server turns the picker on; no tool argument and no model file can.

<!-- no-run: a server that waits for its client -->
```console
pip install "nagasim[mcp,picker]"
naga mcp --root path/to/models --tool-picker gte
```

The first start downloads the model weights (Alibaba-NLP/gte-reranker-modernbert-base, about
600 MB) from Hugging Face and keeps them in its cache; passing the flag is your consent to that
download. A missing package stops the server at startup with the install line. A machine with a GPU
answers in about 0.1 s; for CUDA, install the torch build for your card first. A failure while
ranking keeps `find_tool`'s own order, so the picker can never break the search.

## Hosted: Haiku

Claude Haiku is the more accurate picker, at the cost of a network call per search. It needs the
`anthropic` package and your own Anthropic API key in the environment of the process that starts the server.

<!-- no-run: a server that waits for its client -->
```console
pip install "nagasim[mcp,assistant]"
set ANTHROPIC_API_KEY=...        # or export, on Linux and macOS
naga mcp --root path/to/models --tool-picker haiku
```

Haiku chooses among every tool the server exposes, not only `find_tool`'s ten best, and its three
picks lead the answer, followed by `find_tool`'s own order. Each `find_tool` call sends the query and
the tool catalogue (names, summaries and argument docs) to api.anthropic.com; no model, study or
result data goes with it. That is about 12,000 prompt tokens per call over the 90-tool catalogue it was measured with, mostly read
from Anthropic's prompt cache, since the catalogue is the same for every query. A missing key or
package stops the server at startup with one line; a network error, a timeout (10 s) or an odd reply
keeps `find_tool`'s own order. The key is read from the environment (on Windows, also the value
stored for your user with `setx`, for an app started before you set it), and never logged or answered.

## Offline backup for Haiku

Haiku needs the network and a key, and a local model can cover for both. `--tool-picker-backup PATH`
names a sentence-transformers CrossEncoder folder (or a Hugging Face id) that loads in the background
next to Haiku and ranks every tool whenever Haiku cannot answer. It works only with
`--tool-picker haiku`, and without it nothing changes.

<!-- no-run: a server that waits for its client -->
```console
naga mcp --root path/to/models --tool-picker haiku --tool-picker-backup path/to/cross-encoder
```

A query goes to Haiku first, and only a failed call (no network, a timeout, an API error) is ranked by
the backup instead; if the backup fails too, `find_tool`'s own order stands. With no key or no
`anthropic` package at startup, the server logs one line and runs on the backup alone. Reading the
folder is the only new access, the weights are loaded with remote code off, and the log keeps the
error class, never the key or the query. The backup suits when you keep a trained model of your own offline. A ready-made one, fine-tuned for naga's tools, is on Hugging Face: `--tool-picker-backup TheMattFace/gte-reranker-naga-toolpicker` downloads it on first use ([model card](https://huggingface.co/TheMattFace/gte-reranker-naga-toolpicker)).

A cross-encoder picker (`gte` or the backup) loads in half precision on a GPU and in full precision on CPU. On the frozen test sets half precision picked the same first tool on all 549 requests and ran three times faster on a 4 GB laptop GPU (1.2 s against 3.9 s a decision), while on CPU it ran six times slower (91 s against 15 s). So naga chooses the precision from the hardware, and the model folder's own precision does not matter.

## What the assistant sees

With the picker on, `find_tool` answers the re-ranked list and one more field, `guidance`, which
says to try the first three in order: when a call fails because it was the wrong tool, call the
next one, at most three. There is no hidden retry in the server; the study guide tells the
assistant the same. With the picker off, the answer is unchanged.

## The evidence

The pickers were chosen on two frozen blind sets, never used to tune anything: TEST3, 150 requests written by
four different models, and AGENT3, 274 `find_tool` queries as an agent (Claude Opus 5.5 with naga's server
instructions) phrases them. Each engine below ranked every tool of the 90-tool catalogue; the count is the right tool first.

| Engine | TEST3 (150) | AGENT3 (274) | Median time per query |
|---|---|---|---|
| `find_tool` alone | 36 | 136 | under 1 ms |
| gte-reranker-modernbert-base, untuned | 75 | 161 | 1.1 s |
| gte trained on Claude Opus 5.5's labels (a backup of our own) | 104 | 183 | 1.1 s |
| Claude Haiku 5.5 (`--tool-picker haiku`) | 86 | 190 | 1.0 s |

Times are on a 4 GB laptop GPU, and Haiku's is the API round trip. The `gte` picker re-ranks only
`find_tool`'s ten best, in about 0.1 s: 64 of 150 and 163 of 274 first. On agent queries Haiku and the trained
backup are within noise of each other (Haiku wins 15 queries the backup loses and loses 8), which is why
`--tool-picker haiku` with a trained backup is the setup we run. Retrying the next pick after a wrong one is
what the `guidance` field asks; within three tries even the fastest engines reach the best first try, and telling
the model what the wrong pick did made it worse, so the guidance is a plain retry.

## Another model

The picker ships two models: `gte`, fast and local, and `haiku`, the most accurate, hosted. The backup takes
any CrossEncoder folder, such as one trained on a stronger model's labels with the scripts of the
[System-1 AI Gateway](https://github.com/TheMattHub/Sys1-AI-Gateway); its study
(`docs/studies/naga-decisions.md` there) compares every engine we tried, with the retries chart and the caveats.
Pick the model that fits your hardware and your patience.
