# Research code / 研究代码

Experiments that shaped Skimlight's design. None of this is needed to run the extension.
这些是设计过程中的实验代码，运行扩展时不需要。

Install extra dependencies and run from the repository root with `PYTHONPATH=.` and `OPENROUTER_API_KEY` set:

```bash
.venv/bin/pip install -r research/requirements.txt
export PYTHONPATH=. OPENROUTER_API_KEY=sk-or-...
```

| Script | What it does |
|---|---|
| `python -m skimlight.reader research/data/sample.txt -o out.html` | Offline HTML report with Jev and TF-IDF highlights, adjustable threshold/density |
| `bench.py research/data/sample_en.txt` | Speed and cost of Jev vs general LLMs (Claude Haiku, Gemini Flash-Lite, DeepSeek) on the same candidates |
| `experiments/billing.py` | Measures how Jev bills input tokens (per-request overhead, state counted once, per-question cost) |
| `experiments/word_formats.py` | Precision@k vs token cost of word-level question formats on a hand-labeled set; the chosen format (F4s) comes from here |
| `experiments/formats.py`, `experiments/costopt.py` | Same analysis for the sentence-compression variant |
| `experiments/english_light.py` | spaCy vs the standard-library English candidate rules now used by the host (`skimlight/english.py`): precision@k 96% vs 98%, 107 vs 122 candidates, cost +8%. Dropping spaCy shrank the Windows package from 58 MB to 16 MB. |
| `tsm.py` | Alternative design: grammar-preserving layered fading (GP-TSM style) with Jev as the judge; not used by the extension |

Main findings / 主要结论:

- Jev bills ~260 tokens per request plus the `state` once per request; per-question text is the main lever. Writing the criteria once in `state` and asking `Key word in sN: "w"?` cut cost per question from ~150 to ~18 tokens with no measurable quality loss on the labeled set.
- Batching questions per request matters more than binary-search style multi-round schemes, because every extra round re-pays the `state` and the request overhead.
- Against general LLMs doing the same selection, Jev was 2–10× faster (first block ~0.25 s). Cost was similar to the cheapest LLMs when using the compact format, and Jev never produced out-of-candidate outputs.
