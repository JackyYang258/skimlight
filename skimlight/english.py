"""不依赖 spaCy 的英文分句与候选词生成。

只需要标准库：正则分句（处理常见缩写与小数）、停用词过滤、按大写专名与数字合并短语、否定词合并。
候选词是否重要由 Jev 判断，这里只负责给出合理的候选单元。
units 的拼接结果与原句逐字一致（空白作为 plain 单元保留）。
"""

import re

# 功能词与泛化词：不作为候选
STOP = set("""
a an the this that these those some any each every either neither both all no
i me my mine we us our ours you your yours he him his she her hers it its they them their theirs
who whom whose which what where when why how whether
and or but nor so yet for if then than because while although though unless since until
as at by in on of to from with without within into onto over under about above below after before
between among through during against across along alongside around behind beside besides beyond near next off out up down upon via per
toward towards throughout despite instead whereas
be am is are was were been being have has had having do does did doing done
will would shall should can could may might must ought
not also just only even still very too quite rather really almost already again ever never
here there now then thus hence however therefore moreover furthermore meanwhile otherwise
ones other others another such same own more most less least many much few several lot lots
thing things way ways something anything everything nothing someone anyone everyone
get gets got getting make makes made making take takes took taken go goes went gone going
come comes came use used uses using say says said like
etc e.g i.e vs
""".split())
NEGATIONS = {"not", "no", "never", "without", "nor", "cannot", "none", "neither", "n't"}
ABBREV = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "fig", "figs", "eq",
          "no", "vol", "al", "inc", "ltd", "co", "corp", "u.s", "u.k", "jan", "feb", "mar", "apr", "jun", "jul",
          "aug", "sep", "sept", "oct", "nov", "dec", "approx", "cf", "ca", "p", "pp"}
MAX_PHRASE = 5
MAX_LOWER_PHRASE = 3
# 常见不规则动词的过去式 / 过去分词（小写实词合并时，遇到动词不合并）
IRREGULAR = set("""
arose awoke bore born beat became began bent bet bid bit bled blew broke brought built burnt burst bought caught chose
clung cost crept cut dealt dug dove drew dreamt drank drove ate fell fed felt fought found fled flung flew forbade forgot
forgave froze gave grew hung heard hid hit held hurt kept knelt knew laid led leapt left lent let lay lit lost meant met
paid put quit read rode rang rose ran sawn saw sought sold sent set shook shone shot showed shown shrank shut sang sank sat
slept slid slung spoke sped spent spun spread sprang stood stole stuck stung stank struck strove swore swept swam swung
taught tore told thought threw thrust trod understood woke wore wove wept won wound wrote written
arisen awoken beaten become begun bitten blown broken chosen drawn driven eaten fallen flown forgotten forgiven frozen given
gone grown hidden known lain ridden risen seen shaken spoken stolen sworn taken thrown woken worn
""".split())
L = r"[^\W\d_]"                                       # Unicode 字母（含 é、ö 等）
TOKEN_RE = re.compile(
    r"\s+"                                                  # 空白
    r"|\d+(?:[.,]\d+)*%?(?:-" + L + r"+)?"                  # 数字：1868、3.5、71-lap、40%
    r"|(?:[A-Z]\.){2,}"                                     # 缩写：U.S.、U.K.
    r"|" + L + r"+(?:[-'’]" + L + r"{2,})*(?:n't|n’t)?"     # 单词：don't、O'Brien、well-known；'s 单独切出
    r"|['’]s\b"
    r"|."                                                   # 其他单个字符（标点等）
)


def split_sentences(text):
    """按 . ! ? 分句，跳过缩写、姓名首字母与小数。返回 [(start, end)]。"""
    spans, start = [], 0
    for m in re.finditer(r"[.!?]+[\"'”’)\]]*(?=\s+[\"'“‘(\[]?[A-Z0-9])", text):
        end = m.end()
        prev = re.search(r"([A-Za-z.]+)[.!?]+[\"'”’)\]]*$", text[start:end])
        word = prev.group(1).lower().rstrip(".") if prev else ""
        if m.group(0).startswith(".") and (word in ABBREV or (len(word) == 1 and word.isalpha())):
            continue
        spans.append((start, end))
        start = end
        while start < len(text) and text[start].isspace():
            start += 1
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _kind(tok, first):
    """word / num / neg / stop / punct / space"""
    if tok.isspace():
        return "space"
    low = tok.lower().replace("’", "'")
    if low in NEGATIONS or low.endswith("n't"):
        return "neg"
    if tok[0].isdigit():
        return "num"
    if not tok[0].isalpha():
        return "punct"
    if low in STOP or low in {"'s"} or len(tok) == 1:
        return "stop"
    return "word"


def _is_proper(tok, first):
    return tok[0].isupper() and not first


def _verb_like(tok):
    low = tok.lower()
    return low in IRREGULAR or (len(low) > 4 and low.endswith(("ed", "ing"))) or low.endswith("ly")


def build_units(sentence):
    """把一个句子切成 units：[{t, kind}]，kind ∈ cand / neg / plain。"""
    toks = TOKEN_RE.findall(sentence)
    kinds = []
    seen_word = False
    for t in toks:
        k = _kind(t, not seen_word)
        kinds.append(k)
        if k not in ("space", "punct"):
            seen_word = True
    units = []
    i, n = 0, len(toks)
    first_word_idx = next((j for j, k in enumerate(kinds) if k not in ("space", "punct")), -1)

    def next_word(j):
        """跳过一个空白，返回下一个 token 下标（没有则 None）"""
        if j < n and kinds[j] == "space" and " " in toks[j] and "\n" not in toks[j]:
            j += 1
        return j if j < n else None

    while i < n:
        t, k = toks[i], kinds[i]
        if k == "neg":
            # 否定词与后面紧接的一个实义词合并：not prevent、don't know
            j = next_word(i + 1)
            if j is not None and kinds[j] == "word":
                units.append({"t": "".join(toks[i:j + 1]), "kind": "neg"})
                i = j + 1
            else:
                units.append({"t": t, "kind": "neg"})
                i += 1
            continue
        if k in ("word", "num"):
            # 短语：大写专名连续出现（Formula One World Championship）、数字 + 大写词 / 单位（11 Grand Prix）
            proper = k == "word" and _is_proper(t, i == first_word_idx)
            if k == "word" and i == first_word_idx and t[0].isupper():
                # 句首大写词后面紧跟大写词时按专名处理：Fernando Alonso、Sebastian Vettel
                nj = next_word(i + 1)
                proper = nj is not None and kinds[nj] == "word" and toks[nj][0].isupper()
            j, words = i, 1
            while words < MAX_PHRASE:
                nj = next_word(j + 1)
                if nj is None:
                    break
                nt, nk = toks[nj], kinds[nj]
                if nk == "word" and nt[0].isupper() and (proper or k == "num"):
                    j, words, proper = nj, words + 1, True        # 大写专名 / 数字 + 专名
                    continue
                # 小写实词名词短语：pole position、third place、prime minister；遇到动词或 -ly 副词不合并
                lower_run = not proper and nk == "word" and nt[0].islower() and words < MAX_LOWER_PHRASE
                if lower_run and not _verb_like(t) and not _verb_like(toks[j]) and not _verb_like(nt):
                    j, words = nj, words + 1
                    continue
                break
            units.append({"t": "".join(toks[i:j + 1]), "kind": "cand"})
            i = j + 1
            continue
        units.append({"t": t, "kind": "plain"})
        i += 1
    # 合并相邻的 plain 单元，减小体积
    merged = []
    for u in units:
        if merged and u["kind"] == "plain" and merged[-1]["kind"] == "plain":
            merged[-1]["t"] += u["t"]
        else:
            merged.append(u)
    return merged


def sentences(text):
    """返回 [(start, sentence_text, units)]，units 拼接等于 sentence_text。"""
    out = []
    for s, e in split_sentences(text):
        seg = text[s:e]
        out.append((s, seg, build_units(seg)))
    return out
