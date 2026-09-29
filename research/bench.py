"""速度与成本对比：同一篇文章、同一批候选词、每句选同样数量 k 的词。

方案：
  jev-full     每道 Noul 都带完整判断标准（reader.py 的写法）
  jev-compact  判断标准只在 state 中出现一次，每道题只写词语本身
  llm:<model>  通用大模型，每个文本块一次对话请求，要求输出 JSON

指标：首块返回时间（读者看到第一段高亮前的等待）、全文完成时间、费用、token 数、
与 jev-full 选词的重合率。所有请求不走缓存，块之间并发 8。

用法：OPENROUTER_API_KEY=... python bench.py sample_en.txt --repeat 3
"""

import argparse
import asyncio
import json
import os
import re
import statistics
import time
from pathlib import Path

import httpx

from skimlight import reader

CHAT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODELS = ["anthropic/claude-haiku-4.5", "google/gemini-2.5-flash-lite", "deepseek/deepseek-v4-flash"]
RATIO, KMAX = 0.3, 4

GUIDE = {
    "zh": "对每道题，判断指定句子中的该词语是否属于快速浏览时必须看到的关键词。" + reader.CRITERIA["true"]
          + " 反之：" + reader.CRITERIA["false"],
    "en": "For each question, decide whether the given phrase in the given sentence is a key word a skimming reader "
          "must see. " + reader.CRITERIA_EN["true"] + " Otherwise: " + reader.CRITERIA_EN["false"],
}

LLM_PROMPT = {
    "zh": "你在为速读标注关键词。读者只看每句中被标出的词。{rule}\n"
          "对下面每个句子，从其候选词中恰好选出 k 个，原样抄写候选词。只输出 JSON："
          '{{"s0": ["词1", ...], ...}}，不要其他文字。\n\n{items}',
    "en": "You are marking key words for speed reading. The reader sees only the marked words in each sentence. {rule}\n"
          "For each sentence below, pick exactly k phrases from its candidates, copied verbatim. Output only JSON: "
          '{{"s0": ["phrase", ...], ...}} with no other text.\n\n{items}',
}


def k_for(n):
    return min(KMAX, max(1, round(n * RATIO))) if n else 0


def sentence_cands(block):
    for p in block["paras"]:
        for s in p["sentences"]:
            cands = [u for u in s["units"] if u["kind"] == "cand"]
            yield s, cands


# ---------- 请求构造 ----------

def jev_full_requests(blocks, lang):
    prev = ""
    for b in blocks:
        for body, refs in reader.block_requests(b, prev, lang):
            yield b, body, {q: (qid_sid(q), u["t"]) for q, u in refs.items()}
        prev = b["paras"][-1]["sentences"][-1]["text"]


def qid_sid(q):
    return q.split("_u")[0]


def jev_compact_requests(blocks, lang):
    prev = ""
    for b in blocks:
        sents = [s for p in b["paras"] for s in p["sentences"]]
        state = {"guideline": GUIDE[lang], "section": b["section"], "previous_sentence": prev,
                 "sentences": {s["id"]: s["text"] for s in sents}}
        qs, refs = {}, {}
        for s in sents:
            for k, u in enumerate(s["units"]):
                if u["kind"] == "cand":
                    q = f"{s['id']}_u{k}"
                    qs[q] = {"type": "noul", "instructions": f'{s["id"]}: "{u["t"]}"' if lang == "en"
                             else f"{s['id']}：「{u['t']}」"}
                    refs[q] = (s["id"], u["t"])
        items = list(qs.items())
        for i in range(0, len(items), reader.MAX_QUESTIONS):
            chunk = dict(items[i:i + reader.MAX_QUESTIONS])
            yield b, {"model": reader.MODEL, "state": state, "questions": chunk}, {q: refs[q] for q in chunk}
        prev = sents[-1]["text"]


def llm_request(block, lang, model):
    lines = []
    for s, cands in sentence_cands(block):
        k = k_for(len(cands))
        if k:
            opts = json.dumps(list(dict.fromkeys(u["t"] for u in cands)), ensure_ascii=False)
            lines.append(f'{s["id"]} (k={k}): {s["text"]}\n  candidates: {opts}')
    rule = reader.CRITERIA[("true")] if lang == "zh" else reader.CRITERIA_EN["true"]
    prompt = LLM_PROMPT[lang].format(rule=rule, items="\n".join(lines))
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
            "max_tokens": 2000, "usage": {"include": True}, "reasoning": {"enabled": False}}
    if not model.startswith("anthropic/"):
        body["response_format"] = {"type": "json_object"}
    return body


# ---------- 执行 ----------

async def post(client, url, body):
    for attempt in range(4):
        r = await client.post(url, json=body)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            await asyncio.sleep(1.5 * 2 ** attempt)
            continue
        raise RuntimeError(f"{body.get('model')} HTTP {r.status_code}: {r.text[:300]}")
    raise RuntimeError("retries exhausted")


async def run_jobs(client, jobs, sem):
    """jobs: [(block_index, url, body)]。返回 (结果列表, 首块完成时间, 全部完成时间)。"""
    t0 = time.perf_counter()
    done_at = {}
    remaining = {}
    for bi, _, _ in jobs:
        remaining[bi] = remaining.get(bi, 0) + 1

    async def one(bi, url, body):
        async with sem:
            res = await post(client, url, body)
        remaining[bi] -= 1
        if remaining[bi] == 0:
            done_at[bi] = time.perf_counter() - t0
        return res

    results = await asyncio.gather(*(one(*j) for j in jobs))
    total = time.perf_counter() - t0
    return results, done_at.get(0, total), total


def pick_top_k(probs_by_sid, cands_by_sid):
    out = {}
    for sid, cands in cands_by_sid.items():
        k = k_for(len(cands))
        ranked = sorted(probs_by_sid.get(sid, []), key=lambda x: -x[1])
        chosen = []
        for t, _ in ranked:
            if len(chosen) >= k:
                break
            if t not in chosen:
                chosen.append(t)
        out[sid] = chosen
    return out


def parse_llm_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


async def bench_variant(name, blocks, lang, client, cands_by_sid):
    sem = asyncio.Semaphore(reader.CONCURRENCY)
    index = {id(b): i for i, b in enumerate(blocks)}
    if name.startswith("jev"):
        gen = jev_full_requests if name == "jev-full" else jev_compact_requests
        reqs = list(gen(blocks, lang))
        jobs = [(index[id(b)], reader.ENDPOINT, body) for b, body, _ in reqs]
        results, first, total = await run_jobs(client, jobs, sem)
        probs = {}
        for (_, _, refs), res in zip(reqs, results):
            for q, ans in res["answers"].items():
                sid, t = refs[q]
                probs.setdefault(sid, []).append((t, ans["noul"]))
        chosen = pick_top_k(probs, cands_by_sid)
        usage = [r["usage"] for r in results]
        cost = sum(u.get("cost", 0) or 0 for u in usage)
        tin = sum(u.get("input_tokens", 0) for u in usage)
        tout = sum(u.get("output_tokens", 0) for u in usage)
    else:
        model = name.split(":", 1)[1]
        jobs = [(i, CHAT_ENDPOINT, llm_request(b, lang, model)) for i, b in enumerate(blocks)]
        results, first, total = await run_jobs(client, jobs, sem)
        chosen, n_out, n_valid = {}, 0, 0
        for res in results:
            parsed = parse_llm_json(res["choices"][0]["message"].get("content"))
            for sid, words in parsed.items():
                if sid in cands_by_sid and isinstance(words, list):
                    picks = []
                    for w in words:
                        n_out += 1
                        c = match_candidate(str(w), cands_by_sid[sid])
                        if c:
                            n_valid += 1
                            if c not in picks:
                                picks.append(c)
                    chosen[sid] = picks[:k_for(len(cands_by_sid[sid]))]
        usage = [r.get("usage", {}) for r in results]
        cost = sum(u.get("cost", 0) or 0 for u in usage)
        tin = sum(u.get("prompt_tokens", 0) for u in usage)
        tout = sum(u.get("completion_tokens", 0) for u in usage)
    valid_rate = n_valid / n_out if not name.startswith("jev") and n_out else 1.0
    return {"first": first, "total": total, "cost": cost, "in": tin, "out": tout,
            "requests": len(jobs), "chosen": chosen, "valid": valid_rate}


def match_candidate(w, cands):
    """大模型输出不一定逐字等于候选词：先精确匹配，再按互为子串匹配到最长重叠的候选。"""
    if w in cands:
        return w
    wl = w.lower().strip()
    hits = [c for c in cands if c.lower() in wl or wl in c.lower()]
    return max(hits, key=len) if hits else None


def overlap(a, b, cands_by_sid):
    """每句 |A∩B| / k 的平均值。"""
    vals = []
    for sid, cands in cands_by_sid.items():
        k = k_for(len(cands))
        if k:
            vals.append(len(set(a.get(sid, [])) & set(b.get(sid, []))) / k)
    return sum(vals) / len(vals) if vals else 0


async def main_async(args):
    text = Path(args.input).read_text()
    lang, title, paragraphs, n_sent = reader.segment(text)
    blocks = reader.make_blocks(paragraphs, lang)
    cands_by_sid = {s["id"]: [u["t"] for u in s["units"] if u["kind"] == "cand"]
                    for p in paragraphs if "text" in p for s in p["sentences"]}
    n_cand = sum(len(v) for v in cands_by_sid.values())
    words = len(re.findall(r"\w+", text)) if lang == "en" else len(text)
    print(f"# {title}  lang={lang}  {'words' if lang == 'en' else 'chars'}={words}  "
          f"sentences={n_sent}  candidates={n_cand}  blocks={len(blocks)}  repeat={args.repeat}\n")

    variants = ["jev-full", "jev-compact"] + [f"llm:{m}" for m in args.models]
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"}
    rows = {}
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        for v in variants:
            runs = []
            for _ in range(args.repeat):
                try:
                    runs.append(await bench_variant(v, blocks, lang, client, cands_by_sid))
                except Exception as e:  # 单个模型失败不影响其他方案
                    print(f"{v}: {e}")
                    break
            if runs:
                rows[v] = runs

    ref = rows["jev-full"][0]["chosen"]
    report = []
    hdr = f"{'方案':34s} {'首块(s)':>8s} {'全文(s)':>8s} {'费用($)':>10s} {'输入tok':>8s} {'输出tok':>7s} {'与jev-full重合':>12s} {'输出有效率':>8s}"
    print(hdr)
    print("-" * len(hdr.encode("gbk")))
    for v, runs in rows.items():
        med = lambda key: statistics.median(r[key] for r in runs)
        r0 = runs[0]
        ov = overlap(r0["chosen"], ref, cands_by_sid) if v != "jev-full" else (
            overlap(runs[1]["chosen"], ref, cands_by_sid) if len(runs) > 1 else 1.0)
        line = (f"{v:34s} {med('first'):8.2f} {med('total'):8.2f} {med('cost'):10.6f} "
                f"{int(med('in')):8d} {int(med('out')):7d} {ov:12.0%} {statistics.median(r['valid'] for r in runs):8.0%}")
        print(line)
        report.append({"variant": v, "first_s": med("first"), "total_s": med("total"), "cost_usd": med("cost"),
                       "input_tokens": med("in"), "output_tokens": med("out"), "overlap_vs_jev_full": ov, "valid_rate": statistics.median(r["valid"] for r in runs),
                       "runs": [{k: r[k] for k in ("first", "total", "cost")} for r in runs]})
    out = Path(args.input).with_suffix(".bench.json")
    out.write_text(json.dumps({"title": title, "lang": lang, "sentences": n_sent, "candidates": n_cand,
                               "blocks": len(blocks), "results": report}, ensure_ascii=False, indent=2))
    print(f"\n结果已写入 {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--models", nargs="*", default=LLM_MODELS)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
