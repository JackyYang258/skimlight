"""分层淡化阅读器（GP-TSM 思路，由 Jev 做判断；成本优化后的 V3 流程）。

每个句子逐步删去次要成分，每一步都要求删减后仍是通顺且忠实的句子；越早被删的词显示越淡，
只读最深色的部分就是一段通顺的摘要。只淡化，不隐藏。

流程：
  1. spaCy 依存分析，列出可删成分（状语、介词短语、修饰语、从句、并列项、括号内容）
  2. Jev 第一轮：每个成分一道 Noul「能否删除」，说明写在 state 中，题目只写成分原文
  3. 本地：按可删概率从高到低累积删除，得到逐步变短的前缀序列
  4. Jev 第二轮：每句只验证保留约 85% / 70% / 55% / 40% 长度的检查点（完整任务说明）
  5. 通过验证的检查点构成层级：越早删除越淡，最淡一层不透明度 0.5

用法：
    OPENROUTER_API_KEY=... python tsm.py sample_en.txt -o tsm_en.html
"""

import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import spacy

from skimlight import reader

TARGETS = (0.85, 0.70, 0.55, 0.40)   # 检查点：保留长度占原句的比例
P_DELETE = 0.5                         # 第一轮：可删概率低于该值的成分不参与删除
P_PASS = 0.7                           # 第二轮：验证通过阈值
MAX_Q = 80                             # 每个请求的题目上限
BLOCK_CHARS = {"en": 1500, "zh": 800}  # 每个请求块的正文长度上限
MIN_LEN = {"en": 12, "zh": 25}         # 短于该长度的句子不处理（英文按词，中文按字）
ALPHA_MIN, ALPHA_MAX = 0.50, 0.80      # 淡化层的不透明度范围

DELETABLE = {
    "en": {"advmod", "npadvmod", "prep", "agent", "appos", "acl", "relcl", "advcl", "parataxis", "conj", "amod"},
    "zh": {"advmod", "amod", "nmod", "nmod:prep", "nmod:tmod", "nmod:assmod", "nmod:range", "acl", "advcl:loc",
           "parataxis:prnmod", "appos", "conj"},
}
LEADING = {"conj", "advcl", "appos", "relcl", "acl", "npadvmod", "parataxis"}   # 连带前面的逗号或连词一起删
NEG_WORDS = reader.EN_NEGATIONS | reader.NEGATIONS

GUIDE = ("Each question quotes a span of sentence sN. Answer yes if the span can be deleted and a skimming reader "
         "still gets the key point.")
VERIFY = ("Is the compressed version a grammatical {lang} sentence that stays faithful to sentence {sid} "
          "(keeps its main point, no meaning distorted)?")


# ---------- 文本 → 句子与可删成分 ----------

def length(text, lang):
    return len(re.findall(r"\w+", text)) if lang == "en" else len(re.sub(r"[\W_]", "", text))


def clean(text, lang):
    """删除成分后清理残留的标点与空白。"""
    if lang == "en":
        text = re.sub(r"\s+([,.;:])", r"\1", text)
        text = re.sub(r"([,;:])\s*(?=[,.;:])", "", text)
        text = re.sub(r"\(\s*\)", "", text)
        text = re.sub(r"^\W+", "", text)
        return re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"[，、；：,;:]+(?=[，。；、！？,.;!?])", "", text)
    text = re.sub(r"“\s*”|（\s*）|\(\s*\)", "", text)
    return re.sub(r"^[，、；：。,;:\s]+", "", text).strip()


def find_spans(sent, lang):
    doc = sent.doc
    spans = []
    for t in sent:
        if t.dep_ not in DELETABLE[lang] or t.i == sent.root.i:
            continue
        sub = list(t.subtree)
        a, b = sub[0].i, sub[-1].i
        if b - a + 1 != len(sub):          # 子树不连续，跳过
            continue
        if t.dep_ in LEADING and a - 1 >= sent.start and doc[a - 1].dep_ in {"cc", "punct"}:
            a -= 1
        spans.append((a, b, t.dep_))
    # 括号内容：按规则最先删除
    for m in re.finditer(r"\s*[（(][^）)]*[）)]", sent.text):
        st, en = sent.start_char + m.start(), sent.start_char + m.end()
        toks = [t.i for t in sent if t.idx >= st and t.idx + len(t.text) <= en]
        if toks:
            spans.append((toks[0], toks[-1], "paren"))

    out, seen = [], set()
    n = len(sent)
    for a, b, dep in spans:
        text = doc[a:b + 1].text
        if (a, b) in seen:
            continue
        if dep != "paren":
            if b == a or (lang == "zh" and length(text, lang) < 2):
                continue                    # 单词成分淡化意义小，不出题
            if (b - a + 1) / n > 0.7:
                continue                    # 几乎覆盖整句的成分不考虑
            if any(t.lower_ in NEG_WORDS or t.dep_ == "neg" for t in doc[a:b + 1]):
                continue                    # 含否定词的成分不删
        seen.add((a, b))
        out.append({"a": a, "b": b, "dep": dep, "text": text, "p": 1.0 if dep == "paren" else None})
    return out


def segment(text):
    lang = reader.detect_lang(text)
    nlp = spacy.load("en_core_web_sm" if lang == "en" else "zh_core_web_sm")
    title, paragraphs = reader.parse_document(text)
    sid = 0
    for p in paragraphs:
        if "text" not in p:
            continue
        if lang == "en":
            sents = list(nlp(p["text"]).sents)
        else:
            sents = [nlp(s)[:] for s in reader.split_sentences(p["text"])]
        p["sentences"] = []
        for s in sents:
            if not s.text.strip():
                continue
            active = length(s.text, lang) >= MIN_LEN[lang]
            p["sentences"].append({"id": f"s{sid}", "span": s, "text": s.text.strip(),
                                   "spans": find_spans(s, lang) if active else []})
            sid += 1
    return lang, title, paragraphs


def make_blocks(paragraphs, lang):
    blocks, cur = [], None
    for p in paragraphs:
        if "text" not in p:
            cur = None
            continue
        if cur is None or cur["chars"] + len(p["text"]) > BLOCK_CHARS[lang]:
            cur = {"paras": [], "chars": 0}
            blocks.append(cur)
        cur["paras"].append(p)
        cur["chars"] += len(p["text"])
    return blocks


def rich_state(block):
    sents = [s for p in block["paras"] for s in p["sentences"]]
    return {"paragraph": "\n".join(p["text"] for p in block["paras"]),
            "sentences": {s["id"]: s["text"] for s in sents}}


def chunked(questions):
    items = list(questions.items())
    for i in range(0, len(items), MAX_Q):
        yield dict(items[i:i + MAX_Q])


SOFT_PUNCT = set("，、；：,;:")
HARD_PUNCT = set("。！？.!?")
LINKERS = {"但", "而", "并", "且", "但是", "而且", "并且", "and", "but", "yet", "so"}


def with_orphans(span, drop):
    """删除成分后失去作用的标点一并删除：句首或标点后的逗号、紧接标点或句末的逗号、
    「但 / 而 / but」与被删成分之后的逗号。返回扩充后的删除集合。"""
    drop = set(drop)
    changed = True
    while changed:
        changed = False
        kept = [t for t in span if t.i not in drop]
        for k, t in enumerate(kept):
            if t.text not in SOFT_PUNCT:
                continue
            prev = kept[k - 1] if k > 0 else None
            nxt = kept[k + 1] if k + 1 < len(kept) else None
            gap_before = prev is not None and any(i in drop for i in range(prev.i + 1, t.i))
            if (prev is None or prev.text in SOFT_PUNCT | HARD_PUNCT
                    or nxt is None or nxt.text in SOFT_PUNCT | HARD_PUNCT
                    or (gap_before and prev.lower_ in LINKERS)):
                drop.add(t.i)
                changed = True
                break
    return drop


def render_kept(span, drop, lang):
    return clean("".join(t.text_with_ws for t in span if t.i not in drop), lang)


def prefixes(s, lang):
    order = sorted([x for x in s["spans"] if x["p"] is not None and x["p"] >= P_DELETE],
                   key=lambda x: (-x["p"], x["b"] - x["a"]))
    drop, seq = set(), []
    span = s["span"]
    for x in order:
        rng = set(range(x["a"], x["b"] + 1))
        if rng <= drop:
            continue
        drop = with_orphans(span, drop | rng)
        seq.append((frozenset(drop), render_kept(span, drop, lang)))
    return seq


# ---------- Jev ----------

async def run_round(client, sem, stats, requests):
    results = await asyncio.gather(*(reader.call_jev(client, sem, body, stats) for body in requests))
    for r in results:   # 含缓存命中在内的完整费用，即从零生成本页的费用
        stats["full_cost"] = stats.get("full_cost", 0) + (r["usage"].get("cost") or 0)
        stats["full_input_tokens"] = stats.get("full_input_tokens", 0) + r["usage"]["input_tokens"]
        stats["full_requests"] = stats.get("full_requests", 0) + 1
    return results


async def score(paragraphs, lang, api_key):
    reader.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"requests": 0, "cached": 0, "cost": 0.0, "input_tokens": 0}
    sem = asyncio.Semaphore(reader.CONCURRENCY)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    blocks = make_blocks(paragraphs, lang)
    lang_name = "English" if lang == "en" else "Chinese"

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        # 第一轮：成分能否删除
        reqs, refs = [], []
        for b in blocks:
            state = {"guideline": GUIDE, **rich_state(b)}
            qs, ref = {}, {}
            for p in b["paras"]:
                for s in p["sentences"]:
                    for k, x in enumerate(s["spans"]):
                        if x["dep"] != "paren":
                            qid = f'{s["id"]}.d{k}'
                            qs[qid] = {"type": "noul", "instructions": f'{s["id"]}: "{x["text"]}"'}
                            ref[qid] = x
            for chunk in chunked(qs):
                reqs.append({"model": reader.MODEL, "state": state, "questions": chunk})
                refs.append(ref)
        for res, ref in zip(await run_round(client, sem, stats, reqs), refs):
            for qid, ans in res["answers"].items():
                ref[qid]["p"] = ans["noul"]

        # 第二轮：只验证检查点
        reqs, refs = [], []
        for b in blocks:
            state = rich_state(b)
            qs, ref = {}, {}
            for p in b["paras"]:
                for s in p["sentences"]:
                    seq = prefixes(s, lang)
                    s["seq"] = seq
                    n0 = length(s["text"], lang)
                    picks = []
                    for tg in TARGETS:
                        if not seq:
                            break
                        j = min(range(len(seq)), key=lambda j: abs(length(seq[j][1], lang) / n0 - tg))
                        if j not in picks:
                            picks.append(j)
                    s["checks"] = sorted(picks)
                    for j in s["checks"]:
                        qid = f'{s["id"]}_{j}'
                        qs[qid] = {"type": "noul", "instructions": {
                            "task": VERIFY.format(lang=lang_name, sid=s["id"]), "compressed": seq[j][1]}}
                        ref[qid] = (s, j)
            for chunk in chunked(qs):
                reqs.append({"model": reader.MODEL, "state": state, "questions": chunk})
                refs.append(ref)
        for res, ref in zip(await run_round(client, sem, stats, reqs), refs):
            for qid, ans in res["answers"].items():
                s, j = ref[qid]
                s.setdefault("passed", []).append((j, ans["noul"]))
    return stats, len(blocks)


# ---------- 层级与渲染 ----------

def assign_alpha(s):
    """通过验证的检查点按删除先后分层：最早删除的最淡。返回 [(文本, 不透明度)]。"""
    passed = sorted(j for j, p in s.get("passed", []) if p >= P_PASS)
    alpha = {}
    m = len(passed)
    prev = frozenset()
    for k, j in enumerate(passed):
        drop = s["seq"][j][0]
        a = ALPHA_MIN if m == 1 else ALPHA_MIN + (ALPHA_MAX - ALPHA_MIN) * k / (m - 1)
        for i in drop - prev:
            alpha[i] = round(a, 2)
        prev = drop
    units = []
    for t in s["span"]:
        a = alpha.get(t.i, 1.0)
        if units and units[-1][1] == a:
            units[-1][0] += t.text_with_ws
        else:
            units.append([t.text_with_ws, a])
    core = "".join(u[0] for u in units if u[1] == 1.0)
    return units, core


def render(title, lang, paragraphs, meta):
    doc = []
    for p in paragraphs:
        if "heading" in p:
            doc.append({"h": p["heading"], "l": p["level"]})
            continue
        sents = []
        for s in p["sentences"]:
            units, core = assign_alpha(s)
            sents.append(units)
        doc.append({"s": sents})
    data = json.dumps({"meta": meta, "doc": doc}, ensure_ascii=False).replace("</", "<\\/")
    tpl = (Path(__file__).parent / "tsm_template.html").read_text()
    return (tpl.replace("__TITLE__", html.escape(title)).replace("__DATA__", data)
            .replace("__LANG__", "en" if lang == "en" else "zh-CN"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="tsm.html")
    args = ap.parse_args()
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        sys.exit("OPENROUTER_API_KEY 未设置")

    t0 = time.time()
    lang, title, paragraphs = segment(Path(args.input).read_text())
    t1 = time.time()
    stats, n_blocks = asyncio.run(score(paragraphs, lang, api_key))
    t2 = time.time()

    sents = [s for p in paragraphs if "text" in p for s in p["sentences"]]
    total = sum(length(s["text"], lang) for s in sents)
    core = sum(length(assign_alpha(s)[1], lang) for s in sents)
    faded_sents = sum(1 for s in sents if any(p >= P_PASS for _, p in s.get("passed", [])))
    meta = {"title": title, "lang": lang, "sentences": len(sents), "faded_sentences": faded_sents,
            "core_ratio": round(core / max(1, total), 3), "blocks": n_blocks,
            "parse_seconds": round(t1 - t0, 2), "jev_seconds": round(t2 - t1, 2), "jev": stats}
    Path(args.output).write_text(render(title, lang, paragraphs, meta))
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
