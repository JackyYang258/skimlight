"""Jev 辅助阅读器原型：在每个句子中标出少数几个关键词，用于快速浏览。

流程：分句 → jieba 分词与词性标注 → 合并短语、生成候选词 → 按文本块并发请求 Jev（每个候选词一道 Noul）
→ 将 Jev 概率与 TF-IDF 分数一并写入自包含 HTML，选词在页面端完成，可调阈值与密度。

支持中文（jieba）与英文（spaCy），按正文中汉字占比自动判断。

用法：
    OPENROUTER_API_KEY=... python -m skimlight.reader research/data/sample.txt -o out.html
    python -m skimlight.reader research/data/sample.txt -o out.html --no-jev        # 只生成 TF-IDF 对照
"""

import argparse
import asyncio
import hashlib
import html
import json
import math
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import jieba
import jieba.analyse
import jieba.posseg as pseg

jieba.setLogLevel(60)

ENDPOINT = "https://openrouter.ai/api/v1/systemone"
MODEL = "typesafe/jev-1.13"
CACHE_DIR = Path(__file__).parent / ".cache" / "jev"

MAX_QUESTIONS = 150  # 每个请求的题目上限
BLOCK_CHARS = {"zh": 1200, "en": 2000}   # 每个请求的正文字数上限；state 每个请求只计费一次，块越大越省
CONCURRENCY = 8

NOUNISH = {"n", "nr", "ns", "nt", "nz", "nw", "nrt", "nrfg", "vn", "j", "an"}
NUMERIC = {"m", "q", "mq"}
VERBISH = {"v", "vd", "vi"}
ADJ = {"a", "ad"}
IDIOM = {"i", "l"}
STOP_WORDS = {
    "进行", "成为", "认为", "作为", "表示", "使得", "包括", "具有", "进行", "通过", "得到", "开始", "出现",
    "可以", "能够", "需要", "应该", "已经", "其中", "方面", "问题", "情况", "有关", "相关", "之后", "之前",
}
NEGATIONS = {"不", "未", "没", "没有", "并未", "并非", "不得", "不能", "无法", "从未", "绝不", "无权", "不再",
             "未经", "非", "无", "不可", "不会", "不必", "毋须", "无须"}
MAX_PHRASE = 8
NOUN_SUFFIX = set("州案法权国人者制性党院会省市县区")   # 允许并入短语的单字
CUSTOM_WORDS = set()

EN_STOP_LEMMAS = {"be", "have", "do", "make", "take", "get", "include", "become", "use", "say", "go", "come",
                  "give", "know", "see", "also", "many", "several", "other", "such", "same", "own", "various"}
EN_NEGATIONS = {"not", "n't", "no", "never", "without", "nor", "cannot", "none", "neither"}
EN_MAX_TOKENS = 5
_NLP = None

SENT_RE = re.compile(r"[^。！？；!?;]*[。！？；!?;]+[”」』）)]*|[^。！？；!?;]+$")

INSTRUCTION = "判断句子 {sid} 中的词语「{word}」{occ}是否属于快速浏览时必须看到的关键词。"
INSTRUCTION_EN = 'Is the phrase "{word}"{occ} in sentence {sid} a key word that a skimming reader must see?'
CRITERIA_EN = {
    "true": "The reader sees only one to four marked words per sentence. Without this phrase the core meaning of "
            "the sentence is lost: it is the subject, the main action, the object, or a qualifier that changes the "
            "meaning (time, quantity, condition, target), and it is new information in this sentence.",
    "false": "The phrase can be skipped: it is generic or functional wording, a decorative detail, or information "
             "already given earlier; removing it does not affect the gist of the sentence.",
}
# 实际使用的写法（experiments/word_formats.py 中的 F4s）：判断标准放在 state 中只写一次，每题只写词语。
# 在人工标注集上 precision@k 英文 94%、中文 90%，每题约 18 个输入 token；
# 原写法（每题带完整说明和标准）为 83% / 94%，每题 142–192 token。
GUIDELINE = "yes = " + CRITERIA_EN["true"] + " no = " + CRITERIA_EN["false"]
QUESTION = 'Key word in {sid}: "{word}"{occ}?'

CRITERIA = {
    "true": "读者只看每句中被标出的一至四个词。缺少该词就无法把握该句的核心意思："
            "它是句子的主语、核心动作、宾语，或改变句意的关键限定（时间、数量、条件、对象），且对本句是新信息。",
    "false": "该词可以省略：泛化或功能性的表述、修饰性细节，或上文已明确给出、读者已知的信息；去掉后不影响把握句意。",
}


# ---------- 文本解析 ----------

def parse_document(text):
    """返回 (title, blocks)。blocks 为段落列表，每个段落带所属章节。"""
    lines = text.splitlines()
    title = lines[0].strip() if lines else ""
    section = title
    paragraphs = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(=+)\s*(.+?)\s*=+$", line)
        if m:
            section = m.group(2)
            if section in {"注脚", "参考资料", "外部链接", "参见", "延伸阅读",
                           "References", "Notes", "See also", "External links", "Further reading"}:
                break
            paragraphs.append({"heading": section, "level": len(m.group(1))})
            continue
        paragraphs.append({"section": section, "text": line})
    return title, paragraphs


def add_custom_words(text):
    """把书名号、引号内的短语和章节标题加入词典，避免专有名词被切开。"""
    words = set(re.findall(r"《([^》]{2,16})》", text))
    words |= set(re.findall(r"“([^”]{2,12})”", text))
    words |= {m.group(1) for m in re.finditer(r"^=+\s*(.+?)\s*=+$", text, re.M)}
    for w in words:
        if not re.search(r"[，。、：；\s]", w):
            jieba.add_word(w, freq=20000, tag="nz")
            CUSTOM_WORDS.add(w)


def split_sentences(text):
    return [s for s in SENT_RE.findall(text) if s.strip()]


def is_numeric_word(w):
    """阿拉伯数字，或至少三个字的中文数量（如「四分之三」）；排除「一所」「第一」这类泛指。"""
    return bool(re.search(r"[0-9０-９]", w)) or (len(w) >= 3 and bool(re.search(r"[二三四五六七八九十百千万亿两]", w)))


def build_units(sentence):
    """分词后合并名词短语、数量短语和否定短语。返回 units：[{t, kind}]，kind ∈ cand / neg / plain。"""
    toks = [(w, f) for w, f in pseg.cut(sentence)]

    def joinable(w, f):
        return f in NOUNISH and w not in CUSTOM_WORDS and (len(w) >= 2 or w in NOUN_SUFFIX)

    units = []
    i = 0
    while i < len(toks):
        w, f = toks[i]
        if w in CUSTOM_WORDS:
            units.append({"t": w, "kind": "cand"})
            i += 1
            continue
        if f in NOUNISH and len(w) >= 2:
            j, phrase = i + 1, w
            while j < len(toks) and joinable(*toks[j]) and len(phrase) + len(toks[j][0]) <= MAX_PHRASE:
                phrase += toks[j][0]
                j += 1
            units.append({"t": phrase, "kind": "cand"})
            i = j
            continue
        if f in NUMERIC and is_numeric_word(w):
            j, phrase = i + 1, w
            while j < len(toks) and (toks[j][1] in NUMERIC or toks[j][0] in {"年", "月", "日", "%", "个"}):
                phrase += toks[j][0]
                j += 1
            units.append({"t": phrase, "kind": "cand"})
            i = j
            continue
        if w in NEGATIONS:
            # 否定词与其后的动词、形容词合并，避免只标出「不」而看不到被否定的内容
            phrase, j = w, i + 1
            if j < len(toks) and toks[j][0] in {"能", "会", "得", "可"}:
                phrase += toks[j][0]
                j += 1
            if (j < len(toks) and toks[j][1] in VERBISH | ADJ | NOUNISH and toks[j][0] not in CUSTOM_WORDS
                    and len(phrase) + len(toks[j][0]) <= 5):
                phrase += toks[j][0]
                j += 1
            units.append({"t": phrase, "kind": "neg"})
            i = j
            continue
        if (f in VERBISH or f in ADJ or f in IDIOM) and len(w) >= 2 and w not in STOP_WORDS:
            units.append({"t": w, "kind": "cand"})
        else:
            units.append({"t": w, "kind": "plain"})
        i += 1
    return units


# ---------- 英文 ----------

def detect_lang(text):
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return "zh" if cjk / max(1, len(text)) > 0.1 else "en"


def nlp_en():
    global _NLP
    if _NLP is None:
        import spacy
        _NLP = spacy.load("en_core_web_sm")
    return _NLP


def en_sentences(text):
    return [sent for sent in nlp_en()(text).sents if sent.text.strip()]


def build_units_en(sent):
    """英文：名词短语（去掉限定词、代词，最长 4 词）、实义动词、形容词、数字为候选；否定词与其后的动词或形容词合并。
    units 保留原文空白，拼接后与原句一致。"""
    toks = list(sent)
    chunk_at = {}

    def add_span(ts):
        while ts and (ts[0].pos_ in {"DET", "PRON", "PART", "PUNCT"} or ts[0].lower_ in EN_STOP_LEMMAS):
            ts = ts[1:]
        while ts and ts[-1].pos_ in {"PART", "PUNCT"}:
            ts = ts[:-1]
        if not ts or ts[-1].pos_ == "PRON":
            return
        ts = ts[-EN_MAX_TOKENS:]
        while ts and ts[0].pos_ in {"PART", "PUNCT"}:
            ts = ts[1:]
        if ts:
            chunk_at[ts[0].i] = ts[-1].i

    for nc in sent.noun_chunks:
        ts = list(nc)
        cut = [k for k, t in enumerate(ts) if t.tag_ == "POS"]   # 所有格处拆分：Massa | teammate Kimi Räikkönen
        if cut:
            add_span(ts[:cut[-1]])
            add_span(ts[cut[-1] + 1:])
        else:
            add_span(ts)

    # 未被识别为名词短语的连续专名 / 名词 / 数字（如 11 Grand Prix wins）
    covered = {i for a, b in chunk_at.items() for i in range(a, b + 1)}
    run = []
    for t in toks + [None]:
        if t is not None and t.i not in covered and t.pos_ in {"PROPN", "NOUN", "NUM"} and not t.is_stop:
            run.append(t)
            continue
        if len(run) >= 2:
            add_span(run)
        run = []

    units = []

    def push(start, end, kind):
        span = sent.doc[start:end + 1]
        units.append({"t": span.text, "kind": kind})
        if span[-1].whitespace_:
            units.append({"t": span[-1].whitespace_, "kind": "plain"})

    i = toks[0].i
    last = toks[-1].i
    doc = sent.doc
    while i <= last:
        t = doc[i]
        if i in chunk_at:
            push(i, chunk_at[i], "cand")
            i = chunk_at[i] + 1
            continue
        if t.lower_ in EN_NEGATIONS or t.dep_ == "neg":
            j = i
            if i + 1 <= last and doc[i + 1].pos_ in {"VERB", "ADJ", "AUX"}:
                j = i + 1
                if doc[j].pos_ == "AUX" and j + 1 <= last and doc[j + 1].pos_ in {"VERB", "ADJ"}:
                    j += 1
            a = i
            # 缩写（don't / can't / isn't）：把紧挨着的助动词一并标出，避免只标出 n't
            if t.lower_ in {"n't", "nt"} and i - 1 >= toks[0].i and not doc[i - 1].whitespace_ \
                    and units and units[-1]["t"] == doc[i - 1].text:
                units.pop()
                a = i - 1
            push(a, j, "neg")
            i = j + 1
            continue
        if t.like_num or t.pos_ == "NUM":
            j = i
            while j + 1 <= last and (doc[j + 1].like_num or doc[j + 1].pos_ == "NUM"):
                j += 1
            push(i, j, "cand")
            i = j + 1
            continue
        content = (t.pos_ in {"VERB", "ADJ", "PROPN", "NOUN"} and t.lemma_.lower() not in EN_STOP_LEMMAS
                   and not t.is_stop and len(t.text) > 1)
        push(i, i, "cand" if content else "plain")
        i += 1
    return units


def tfidf_scorer_en(all_text):
    from wordfreq import zipf_frequency
    words = re.findall(r"[A-Za-zÀ-ÿ']+", all_text.lower())
    counts = Counter(words)
    total = max(1, len(words))

    def score(phrase):
        parts = re.findall(r"[A-Za-zÀ-ÿ']+", phrase.lower()) or [phrase.lower()]
        idf = max(8 - zipf_frequency(w, "en") for w in parts)   # 越罕见越高
        tf = max(counts.get(w, 0) for w in parts) / total
        return round(tf * idf * 1000, 4)

    return score


# ---------- TF-IDF 对照 ----------

def tfidf_scorer(all_text):
    idf, median = jieba.analyse.default_tfidf.idf_loader.get_idf()
    counts = Counter(w for w in jieba.cut(all_text))
    total = sum(counts.values())

    def score(phrase):
        parts = [w for w in jieba.cut(phrase)] or [phrase]
        w_idf = max(idf.get(p, median) for p in parts)
        tf = max(counts.get(phrase, 0), min(counts.get(p, 0) for p in parts), 1) / total
        return round(tf * w_idf * 1000, 4)

    return score


# ---------- Jev 请求 ----------

def make_blocks(paragraphs, lang="zh"):
    """把同一章节内相邻的段落合并为请求单元，控制字数和题目数。"""
    blocks, cur = [], None
    for p in paragraphs:
        if "heading" in p:
            cur = None
            continue
        n_cand = sum(1 for s in p["sentences"] for u in s["units"] if u["kind"] == "cand")
        chars = len(p["text"])
        if (cur is None or cur["section"] != p["section"] or cur["chars"] + chars > BLOCK_CHARS[lang]
                or cur["n_cand"] + n_cand > MAX_QUESTIONS):
            cur = {"section": p["section"], "paras": [], "chars": 0, "n_cand": 0}
            blocks.append(cur)
        cur["paras"].append(p)
        cur["chars"] += chars
        cur["n_cand"] += n_cand
    return blocks


def block_requests(block, prev_sentence, lang="zh"):
    """一个块可能因题目过多拆成多个请求，但每个请求都带上整块正文作为 state。"""
    sents = [s for p in block["paras"] for s in p["sentences"]]
    state = {
        "guideline": GUIDELINE,
        "section": block["section"],
        "previous_sentence": prev_sentence,
        "sentences": {s["id"]: s["text"] for s in sents},
    }
    questions, refs = {}, {}
    for s in sents:
        seen = Counter()
        for k, u in enumerate(s["units"]):
            if u["kind"] != "cand":
                continue
            seen[u["t"]] += 1
            total = sum(1 for x in s["units"] if x["t"] == u["t"])
            qid = f"{s['id']}_u{k}"
            occ = f" (occurrence {seen[u['t']]})" if total > 1 else ""
            questions[qid] = {"type": "noul", "instructions": QUESTION.format(sid=s["id"], word=u["t"], occ=occ)}
            refs[qid] = u
    items = list(questions.items())
    for i in range(0, len(items), MAX_QUESTIONS):
        chunk = dict(items[i:i + MAX_QUESTIONS])
        yield {"model": MODEL, "state": state, "questions": chunk}, {q: refs[q] for q in chunk}


async def call_jev(client, sem, body, stats):
    key = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        stats["cached"] += 1
        return json.loads(path.read_text())
    async with sem:
        for attempt in range(4):
            try:
                r = await client.post(ENDPOINT, json=body)
                if r.status_code == 200:
                    data = r.json()
                    path.write_text(json.dumps(data, ensure_ascii=False))
                    stats["requests"] += 1
                    stats["cost"] += data.get("usage", {}).get("cost", 0) or 0
                    stats["input_tokens"] += data.get("usage", {}).get("input_tokens", 0)
                    return data
                if r.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(1.5 * 2 ** attempt)
                    continue
                raise RuntimeError(f"Jev HTTP {r.status_code}: {r.text[:300]}")
            except (httpx.TimeoutException, httpx.TransportError):
                await asyncio.sleep(1.5 * 2 ** attempt)
        raise RuntimeError("Jev request failed after retries")


async def score_with_jev(blocks, api_key, lang="zh"):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"requests": 0, "cached": 0, "cost": 0.0, "input_tokens": 0}
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    jobs = []
    prev = ""
    for b in blocks:
        for body, refs in block_requests(b, prev, lang):
            jobs.append((body, refs))
        prev = b["paras"][-1]["sentences"][-1]["text"] if b["paras"][-1]["sentences"] else prev
    async with httpx.AsyncClient(headers=headers, timeout=60) as client:
        results = await asyncio.gather(*(call_jev(client, sem, body, stats) for body, _ in jobs))
    # 含缓存命中在内的完整费用，即从零生成本页的费用
    stats["full_cost"] = sum(r["usage"].get("cost") or 0 for r in results)
    stats["full_input_tokens"] = sum(r["usage"]["input_tokens"] for r in results)
    for (_, refs), data in zip(jobs, results):
        for qid, ans in data["answers"].items():
            refs[qid]["p"] = round(ans["noul"], 3)
    return stats, len(jobs)


# ---------- 主流程 ----------

def segment(text):
    """解析、分句、生成候选词与 TF-IDF 分数，不调用 Jev。"""
    lang = detect_lang(text)
    if lang == "zh":
        add_custom_words(text)
    title, paragraphs = parse_document(text)
    body_text = "\n".join(p["text"] for p in paragraphs if "text" in p)
    tfidf = tfidf_scorer(body_text) if lang == "zh" else tfidf_scorer_en(body_text)

    sid = 0
    for p in paragraphs:
        if "text" not in p:
            continue
        p["sentences"] = []
        sents = split_sentences(p["text"]) if lang == "zh" else en_sentences(p["text"])
        for s in sents:
            units = build_units(s) if lang == "zh" else build_units_en(s)
            for u in units:
                if u["kind"] == "cand":
                    u["tf"] = tfidf(u["t"])
            text_s = s if lang == "zh" else s.text_with_ws
            p["sentences"].append({"id": f"s{sid}", "text": text_s.strip(), "units": units})
            sid += 1
    return lang, title, paragraphs, sid


def process(text, use_jev, api_key):
    lang, title, paragraphs, sid = segment(text)
    blocks = make_blocks(paragraphs, lang)
    stats, n_req = {}, 0
    if use_jev:
        t0 = time.time()
        stats, n_req = asyncio.run(score_with_jev(blocks, api_key, lang))
        stats["seconds"] = round(time.time() - t0, 2)

    n_cand = sum(1 for p in paragraphs if "text" in p for s in p["sentences"] for u in s["units"] if u["kind"] == "cand")
    meta = {"title": title, "lang": lang, "sentences": sid, "candidates": n_cand, "blocks": len(blocks),
            "jev_requests": n_req, "jev": stats, "model": MODEL if use_jev else None}
    return title, paragraphs, meta


def render_html(title, paragraphs, meta):
    doc = []
    for p in paragraphs:
        if "heading" in p:
            doc.append({"h": p["heading"], "l": p["level"]})
            continue
        doc.append({"s": [[[u["t"], {"cand": 1, "neg": 2}.get(u["kind"], 0), u.get("p"), u.get("tf")]
                           for u in s["units"]] for s in p["sentences"]]})
    data = json.dumps({"meta": meta, "doc": doc}, ensure_ascii=False).replace("</", "<\\/")
    template = (Path(__file__).parent / "template.html").read_text()
    return (template.replace("__TITLE__", html.escape(title)).replace("__DATA__", data)
            .replace("__LANG__", "en" if meta.get("lang") == "en" else "zh-CN"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="out.html")
    ap.add_argument("--no-jev", action="store_true")
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not args.no_jev and not api_key:
        sys.exit("OPENROUTER_API_KEY 未设置；或使用 --no-jev 只生成 TF-IDF 对照。")

    text = Path(args.input).read_text()
    title, paragraphs, meta = process(text, not args.no_jev, api_key)
    Path(args.output).write_text(render_html(title, paragraphs, meta))
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
