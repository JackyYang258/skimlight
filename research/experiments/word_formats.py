"""词语层面：在人工标注集上比较题目写法的 precision@k 与每题输入 token。
每句取概率最高的 k 个候选词（k 同 reader.py 规则），看落在「可接受关键词」集合中的比例。"""
import os, statistics, httpx
from pathlib import Path
from skimlight import reader

URL = "https://openrouter.ai/api/v1/systemone"
DATA = Path(__file__).resolve().parents[1] / "data"
H = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}
LABELS = {
 str(DATA / "sample_en.txt"): {
  "s0": {"2008 Brazilian Grand Prix", "Formula One motor race", "2 November", "São Paulo", "Autódromo José Carlos Pace"},
  "s1": {"eighteenth and final race"},
  "s2": {"Ferrari driver Felipe Massa", "won", "11 Grand Prix"},
  "s3": {"Fernando Alonso", "second", "teammate Kimi Räikkönen", "third"},
  "s4": {"Massa", "started", "Toyota driver Jarno Trulli"},
  "s5": {"teammate Räikkönen", "third", "McLaren driver Lewis Hamilton"},
  "s6": {"Rain", "delaying", "start", "dried", "Massa", "established", "lead"},
  "s7": {"More rain", "treacherous", "Massa", "winning", "last few laps"},
  "s8": {"Sebastian Vettel", "fourth place"},
  "s9": {"Hamilton", "passed", "Timo Glock", "fifth", "points", "Championship"},
  "s10": {"Hamilton", "praise", "Damon Hill", "Michael Schumacher", "Formula One community"},
  "s11": {"McLaren driver", "official congratulations", "Queen Elizabeth II", "British prime minister Gordon Brown"},
  "s12": {"Ferrari", "Constructors", "Championship"},
  "s13": {"Grand Prix winner David Coulthard", "final race", "retired", "246 race"}},
 str(DATA / "sample.txt"): {
  "s1": {"公民权利", "平等", "保护", "南北战争", "奴隶"},
  "s6": {"第五款", "赋予", "国会执法权", "引用"},
  "s8": {"公民权", "定义", "推翻", "美国最高法院", "斯科特", "桑福德案案", "非洲奴隶", "美国公民", "判决", "宽泛"},
  "s30": {"五分之三", "妥协", "黑奴", "第十三", "联邦众议院议席", "增长", "戏剧性"},
  "s40": {"草案", "种族", "禁止", "公民投票", "国会议席", "人口数", "计算"},
  "s90": {"7月28日", "国务卿", "宣布", "修正案", "撤消"},
  "s120": {"联邦最高法院", "保留地", "印第安人", "管辖", "美国公民身份", "美国公民", "出生"},
  "s150": {"大法官托马斯", "特权或豁免权条款", "全面恢复"},
  "s200": {"2007年", "法院", "家长", "种族因素", "公立学校念书", "裁定"},
  "s230": {"第三款", "防止", "美国社会党成员", "维克多", "伯格", "当选", "联邦众议员", "违反", "间谍法", "军国主义观点"}},
}
GUIDE = {"en": "Each question quotes a word or phrase of sentence sN. Answer yes if a reader who sees only one to four "
               "marked words per sentence must see it to get the core meaning of sN (subject, main action, object, or "
               "a qualifier that changes the meaning) and it is new information.",
         "zh": "Each question quotes a word or phrase of sentence sN. Answer yes if a reader who sees only one to four "
               "marked words per sentence must see it to get the core meaning of sN (subject, main action, object, or "
               "a qualifier that changes the meaning) and it is new information."}


def k_for(n):
    return min(4, max(1, round(n * 0.3))) if n else 0


def post(state, qs):
    r = httpx.post(URL, headers=H, json={"model": reader.MODEL, "state": state, "questions": qs}, timeout=120).json()
    return r["answers"], r["usage"]["input_tokens"]


def main():
    rows = {}
    for f, labels in LABELS.items():
        lang, title, paras, _ = reader.segment(Path(f).read_text())
        sents = {s["id"]: s for p in paras if "text" in p for s in p["sentences"] if s["id"] in labels}
        cands = {sid: list(dict.fromkeys(u["t"] for u in s["units"] if u["kind"] == "cand")) for sid, s in sents.items()}
        tf = {sid: {u["t"]: u["tf"] for u in s["units"] if u["kind"] == "cand"} for sid, s in sents.items()}
        text = {sid: s["text"] for sid, s in sents.items()}
        para = " ".join(text.values()) if lang == "en" else "".join(text.values())
        instr = reader.INSTRUCTION_EN if lang == "en" else reader.INSTRUCTION
        crit = reader.CRITERIA_EN if lang == "en" else reader.CRITERIA
        fmts = {
            "F0 每题完整说明+标准（现 reader.py）": ({"section": title, "sentences": text},
                lambda sid, w: {"type": "noul", "instructions": instr.format(sid=sid, word=w, occ=""), "criteria": crit}),
            "F1 编号+state 指南": ({"guideline": GUIDE[lang], "sentences": text},
                lambda sid, w: {"type": "noul", "instructions": f'{sid}: "{w}"'}),
            "F2 编号+state 指南+连贯段落": ({"guideline": GUIDE[lang], "paragraph": para, "sentences": text},
                lambda sid, w: {"type": "noul", "instructions": f'{sid}: "{w}"'}),
            "F3 短问句+连贯段落": ({"paragraph": para, "sentences": text},
                lambda sid, w: {"type": "noul", "instructions": f'Must a skimming reader see "{w}" to get the core meaning of {sid}?'}),
        }
        crit_text = f"yes = {crit['true']} no = {crit['false']}"
        crit_en = f"yes = {reader.CRITERIA_EN['true']} no = {reader.CRITERIA_EN['false']}"
        fmts = {
            "F0 每题完整说明+标准": fmts["F0 每题完整说明+标准（现 reader.py）"],
            "F4 完整问句，标准放 state": ({"guideline": crit_text, "section": title, "sentences": text},
                lambda sid, w: {"type": "noul", "instructions": instr.format(sid=sid, word=w, occ="")}),
            "F4e 英文完整问句，英文标准放 state": ({"guideline": crit_en, "section": title, "sentences": text},
                lambda sid, w: {"type": "noul", "instructions": reader.INSTRUCTION_EN.format(sid=sid, word=w, occ="")}),
            "F4s 短问句，英文标准放 state": ({"guideline": crit_en, "section": title, "sentences": text},
                lambda sid, w: {"type": "noul", "instructions": f'Key word in {sid}: "{w}"?'}),
        }
        base_prec = []
        for sid, c in cands.items():
            k = k_for(len(c)); top = sorted(c, key=lambda w: -tf[sid][w])[:k]
            base_prec.append(sum(w in LABELS[f][sid] for w in top) / k)
        rows.setdefault("TF-IDF 基线", {})[lang] = (statistics.mean(base_prec), 0.0)
        for name, (state, q) in fmts.items():
            _, base_tok = post(state, {"z": {"type": "noul", "instructions": "x"}})
            qs, ref = {}, {}
            for sid, c in cands.items():
                for j, w in enumerate(c):
                    qs[f"{sid}_{j}"] = q(sid, w); ref[f"{sid}_{j}"] = (sid, w)
            runs = []
            for _ in range(2):
                ans, tok = post(state, qs)
                probs = {}
                for qid, a in ans.items():
                    sid, w = ref[qid]; probs.setdefault(sid, []).append((w, a["noul"]))
                prec = []
                for sid, c in cands.items():
                    k = k_for(len(c)); top = [w for w, _ in sorted(probs[sid], key=lambda x: -x[1])[:k]]
                    prec.append(sum(w in LABELS[f][sid] for w in top) / k)
                runs.append(statistics.mean(prec))
            rows.setdefault(name, {})[lang] = (statistics.mean(runs), (tok - base_tok) / len(qs), runs)
            continue
            probs = {}
            for qid, a in ans.items():
                sid, w = ref[qid]; probs.setdefault(sid, []).append((w, a["noul"]))
            prec = []
            for sid, c in cands.items():
                k = k_for(len(c)); top = [w for w, _ in sorted(probs[sid], key=lambda x: -x[1])[:k]]
                prec.append(sum(w in LABELS[f][sid] for w in top) / k)
            rows.setdefault(name, {})[lang] = (statistics.mean(prec), (tok - base_tok) / len(qs))
    print(f"{'写法':34s} {'英文 P@k':>9s} {'中文 P@k':>9s} {'英文 tok/题':>11s} {'中文 tok/题':>11s}")
    for name, r in rows.items():
        spread = lambda x: "" if len(x) < 3 else f"({x[2][0]:.0%}/{x[2][1]:.0%})"
        print(f"{name:34s} {r['en'][0]:6.0%}{spread(r['en']):>12s} {r['zh'][0]:6.0%}{spread(r['zh']):>12s} {r['en'][1]:9.1f} {r['zh'][1]:9.1f}")


main()
