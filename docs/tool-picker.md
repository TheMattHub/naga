# Pick tools with a small model (optional)

`find_tool` ranks the agent tools by plain word overlap, and a small re-ranking model can put the
right tool first more often. The tool picker is that model: an optional extra that reorders
`find_tool`'s ten best candidates. It is off by default, so a new install behaves exactly as before.

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

Times are on a 4 GB laptop GPU. Retrying the next pick after a wrong one equals the top-3 column;
telling the model what the wrong pick did made it worse, so the guidance is a plain retry. Fine-tuning
gte on our own requests did not hold on the blind set, so the picker is the model as published.

## Another model

The picker ships one model because it was the best fast one on the bench, not the best overall.
If you prefer another (Gemma is more accurate and slower, Laya is faster), the
[System-1 AI Gateway](https://github.com/TheMattHub/Sys1-AI-Gateway) runs these and others behind one
interface, and its study report (`docs/studies/naga-decisions.md` there) has the full comparison.
Pick the model that fits your hardware and your patience.
