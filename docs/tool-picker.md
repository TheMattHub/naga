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
result data goes with it. That is about 12,000 prompt tokens per call over 90 tools, mostly read
from Anthropic's prompt cache, since the catalogue is the same for every query. A missing key or
package stops the server at startup with one line; a network error, a timeout (10 s) or an odd reply
keeps `find_tool`'s own order. The key is read from the environment (on Windows, also the value
stored for your user with `setx`, for an app started before you set it), and never logged or answered.

## What the assistant sees

With the picker on, `find_tool` answers the re-ranked list and one more field, `guidance`, which
says to try the first three in order: when a call fails because it was the wrong tool, call the
next one, at most three. There is no hidden retry in the server; the study guide tells the
assistant the same. With the picker off, the answer is unchanged.

## The evidence

The picker was chosen on a frozen blind set: 25 requests written from the tool names only, never
used to tune anything. Each engine re-ranked `find_tool`'s top ten.

| Engine | Right tool first | Right tool in top 3 | Median time per request |
|---|---|---|---|
| `find_tool` alone | 8 of 25 | 14 of 25 | under 1 ms |
| gte-reranker-modernbert-base (the picker) | 14 of 25 | 17 of 25 | 96 ms |
| Qwen3-Reranker-0.6B | 14 of 25 | 19 of 25 | 513 ms |
| Gemma 4 E4B (Q4) | 18 of 25 | 19 of 25 | 2.6 s |
| Laya (System-1) | 13 of 25 | 17 of 25 | 66 ms |
| Claude Haiku 5.5 (hosted API) | 19 of 25 | 19 of 25 | 0.9 s |
| Claude Haiku 5.5, choosing among all 90 tools (`--tool-picker haiku`) | 22 of 25 | 23 of 25 | 0.9 s |

Times are on a 4 GB laptop GPU, and Haiku's is the API round trip. Haiku reached the ceiling of
the top ten (19 of the 25 right tools are in it), which is why the `haiku` picker chooses among all
the tools instead; it sends each request to Anthropic, so it is opt-in like `gte`. Retrying the next pick after a wrong one equals the top-3 column;
telling the model what the wrong pick did made it worse, so the guidance is a plain retry. Fine-tuning
gte on our own requests did not hold on the blind set, so the picker is the model as published.

## Another model

The picker ships two models: `gte`, the best fast local one on the bench, and `haiku`, the most
accurate, hosted. If you prefer another (Gemma is more accurate than gte and slower, Laya is faster), the
[System-1 AI Gateway](https://github.com/TheMattHub/Sys1-AI-Gateway) runs these and others behind one
interface, and its study report (`docs/studies/naga-decisions.md` there) has the full comparison.
Pick the model that fits your hardware and your patience.
