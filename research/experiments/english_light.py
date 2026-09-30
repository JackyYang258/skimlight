"""英文候选词：spaCy 方案与无 spaCy 的轻量方案（skimlight/english.py）在人工标注集上的比较。
指标：precision@k（选出的词与标注关键词有共同实词即算选对）、候选词数（决定题目数与费用）、实际费用。
用法：PYTHONPATH=. OPENROUTER_API_KEY=... python research/experiments/english_light.py"""
import os, re, statistics, sys
from pathlib import Path
import httpx
from skimlight import reader, english
sys.path.insert(0, str(Path(__file__).parent))
from word_formats import LABELS, k_for, DATA  # noqa: E402

H = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}
labels = LABELS[str(DATA / "sample_en.txt")]
text = (DATA / "sample_en.txt").read_text().split("\n", 2)[2]
words = lambda t: {w.lower() for w in re.findall(r"[^\W\d_]{3,}|\d+", t)} - english.STOP


def sentences_spacy():
    out = []
    for para in [l for l in text.splitlines() if l.strip()]:
        for sent in reader.nlp_en()(para).sents:
            out.append({"text": sent.text.strip(), "units": reader.build_units_en(sent)})
    return out


def sentences_light():
    return [{"text": seg.strip(), "units": units}
            for para in [l for l in text.splitlines() if l.strip()] for _, seg, units in english.sentences(para)]


def evaluate(sents):
    for i, s in enumerate(sents):
        s["id"] = f"s{i}"
    block = {"section": "2008 Brazilian Grand Prix", "paras": [{"sentences": sents}]}
    probs, cost, n_q = {}, 0.0, 0
    for body, refs in reader.block_requests(block, "", "en"):
        r = httpx.post(reader.ENDPOINT, json=body, headers=H, timeout=120).json()
        cost += r["usage"]["cost"]; n_q += len(body["questions"])
        for q, a in r["answers"].items():
            sid = q.split("_u")[0]; probs.setdefault(sid, []).append((refs[q]["t"], a["noul"]))
    prec = []
    for s in sents:
        cands = list(dict.fromkeys(u["t"] for u in s["units"] if u["kind"] == "cand"))
        k = k_for(len(cands))
        if not k or s["id"] not in labels:
            continue
        top = [w for w, _ in sorted(dict(probs[s["id"]]).items(), key=lambda x: -x[1])[:k]]
        gold = set().union(*(words(g) for g in labels[s["id"]]))
        prec.append(sum(bool(words(w) & gold) for w in top) / k)
    return statistics.mean(prec), n_q, cost


for name, fn in (("spaCy", sentences_spacy), ("轻量（无 spaCy）", sentences_light)):
    runs = [evaluate(fn()) for _ in range(2)]
    print(f"{name:14s} precision@k {statistics.mean(r[0] for r in runs):5.0%}（{runs[0][0]:.0%} / {runs[1][0]:.0%}）"
          f"  候选词 {runs[0][1]}  费用 ${statistics.mean(r[2] for r in runs):.6f}")
