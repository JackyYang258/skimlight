r"""Skimlight 的 Chrome Native Messaging 本地程序：接收网页段落，返回每个候选词的字符位置与 Jev 概率。

协议：标准输入输出，每条消息前 4 字节小端长度，正文为 UTF-8 JSON。
请求  {"id", "type": "highlight", "page": {"title", "url"}, "blocks": [{"id", "text"}]}
      {"id", "type": "ping"}
      {"id", "type": "stats"}   费用统计：今日 / 近 7 天 / 近 30 天 / 累计、近 14 天逐日、近 30 天各网站
      {"id", "type": "get_config"}   返回服务商、每日上限、是否已配置 key（只返回 key 末 4 位）
      {"id", "type": "set_config", "provider", "api_key"(可省略，省略则保留原值), "daily_budget_usd"}
      {"id", "type": "test_key", "provider"?, "api_key"?}   用 1 道题的请求验证 key（不传则用已保存的配置）
返回  {"id", "blocks": [{"id", "lang", "sentences": [[s, e]], "cands": [[s, e, p, kind]]}],
       "usage": {"cost", "input_tokens", "requests", "cached_blocks"}}
      kind：1 为候选词（p 为 Jev 概率），2 为否定词（按规则始终标出，p 为 null）
出错时返回 {"id", "error": "..."}。

stdout 只用于协议消息。配置、缓存与日志的位置：
  Windows          %LOCALAPPDATA%\Skimlight\data\（config.json、cache.sqlite、host.log）
  Linux / WSL      ~/.config/skimlight/config.json，~/.cache/skimlight/（cache.sqlite、host.log）
"""

import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from skimlight import english, reader  # noqa: E402

PROMPT_VERSION = "f4s-3"          # 题目写法或候选词规则变化时修改，使旧缓存失效（f4s-3：英文改用无 spaCy 的规则方案）
if os.name == "nt":
    CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Skimlight" / "data"
    CONFIG_PATH = CACHE_DIR / "config.json"
else:
    CACHE_DIR = Path.home() / ".cache" / "skimlight"
    CONFIG_PATH = Path.home() / ".config" / "skimlight" / "config.json"

# 服务商：OpenRouter 返回的用量中带费用；TypeSafe 官方不带，按公开价格计算
PRICE_PER_INPUT_TOKEN = 0.042 / 1e6
PROVIDERS = {
    "openrouter": {"name": "OpenRouter", "endpoint": "https://openrouter.ai/api/v1/systemone", "model": "typesafe/jev-1.13"},
    "typesafe": {"name": "TypeSafe", "endpoint": "https://api.typesafe.ai/v1/systemone", "model": "jev-latest"},
}
MIN_BLOCK_CHARS = 20              # 去掉空白后短于该长度的段落不处理
JEV_CONCURRENCY = 8

CACHE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=CACHE_DIR / "host.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("skimlight-host")


# ---------- 配置、缓存与费用 ----------

CONFIG_LOCK = threading.Lock()


def load_config():
    cfg = {"daily_budget_usd": 0.5, "provider": "openrouter", "api_key": ""}
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if cfg["provider"] not in PROVIDERS:
        cfg["provider"] = "openrouter"
    env = "OPENROUTER_API_KEY" if cfg["provider"] == "openrouter" else "TYPESAFE_API_KEY"
    cfg["api_key"] = (cfg.get("api_key") or os.environ.get(env, "")).strip()
    return cfg


def save_config(cfg):
    """写入配置文件；Linux / WSL 下权限设为 600（Windows 下 %LOCALAPPDATA% 默认只有本用户可访问）。"""
    data = {k: cfg[k] for k in ("provider", "api_key", "daily_budget_usd")}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with CONFIG_LOCK:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)


def public_config(cfg):
    key = cfg["api_key"]
    return {"ok": True, "provider": cfg["provider"], "providers": {k: v["name"] for k, v in PROVIDERS.items()},
            "daily_budget_usd": cfg["daily_budget_usd"], "has_key": bool(key),
            "key_hint": f"…{key[-4:]}" if len(key) >= 8 else "", "config_path": str(CONFIG_PATH)}


def handle_set_config(msg):
    cfg = load_config()
    if msg.get("provider") in PROVIDERS:
        cfg["provider"] = msg["provider"]
    if msg.get("api_key"):
        key = str(msg["api_key"]).strip()
        if not key.isascii() or any(c.isspace() for c in key):
            return {"error": "API key 只能包含 ASCII 字符，且不能含空白"}
        cfg["api_key"] = key
    if msg.get("daily_budget_usd") is not None:
        budget = float(msg["daily_budget_usd"])
        if not 0 < budget <= 100:
            return {"error": "每日费用上限需在 0 到 100 美元之间"}
        cfg["daily_budget_usd"] = budget
    save_config(cfg)
    log.info("config saved (provider=%s, has_key=%s)", cfg["provider"], bool(cfg["api_key"]))
    return public_config(cfg)


def handle_test_key(msg):
    """发送 1 道题的请求验证 key 与服务商是否可用，费用约 $0.00001。"""
    cfg = load_config()
    provider = msg.get("provider") if msg.get("provider") in PROVIDERS else cfg["provider"]
    key = (msg.get("api_key") or "").strip() or (cfg["api_key"] if provider == cfg["provider"] else "")
    if not key:
        return {"error": "没有可测试的 API key"}
    prov = PROVIDERS[provider]
    body = {"model": prov["model"], "state": "The sky is blue.",
            "questions": {"q": {"type": "noul", "instructions": "Is the sky blue?"}}}
    t0 = time.time()
    try:
        r = httpx.post(prov["endpoint"], json=body, timeout=30,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        return {"error": f"无法连接 {prov['name']}：{e}"}
    if r.status_code != 200:
        return {"error": f"{prov['name']} 返回 HTTP {r.status_code}：{r.text[:200]}"}
    data = r.json()
    return {"ok": True, "provider": provider, "model": data.get("model"), "latency_ms": round((time.time() - t0) * 1000),
            "answer": data["answers"]["q"]["noul"]}


class Store:
    """结果缓存与费用记录。费用按（日期, 网站）累计，日期为本地时区。"""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS blocks (key TEXT PRIMARY KEY, result TEXT)")
            # blocks：新处理（需请求 Jev）的段数；cached_blocks：命中缓存的段数
            self.db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT, site TEXT, cost REAL, requests INTEGER, "
                            "input_tokens INTEGER, blocks INTEGER, cached_blocks INTEGER, PRIMARY KEY (day, site))")
            # 旧版只按日记录（spend 表），迁移为网站未知的记录
            if self.db.execute("SELECT name FROM sqlite_master WHERE name='spend'").fetchone():
                self.db.execute("INSERT OR IGNORE INTO usage SELECT day, '', cost, requests, 0, 0, 0 FROM spend")
                self.db.execute("DROP TABLE spend")
            self.db.commit()

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT result FROM blocks WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, result):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO blocks VALUES (?, ?)", (key, json.dumps(result)))
            self.db.commit()

    def today(self):
        day = datetime.date.today().isoformat()
        with self.lock:
            row = self.db.execute("SELECT COALESCE(SUM(cost), 0), COALESCE(SUM(requests), 0) FROM usage WHERE day=?",
                                  (day,)).fetchone()
        return row

    def add_usage(self, site, cost, requests, input_tokens, blocks, cached_blocks):
        day = datetime.date.today().isoformat()
        with self.lock:
            self.db.execute(
                "INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(day, site) DO UPDATE SET "
                "cost = cost + excluded.cost, requests = requests + excluded.requests, "
                "input_tokens = input_tokens + excluded.input_tokens, blocks = blocks + excluded.blocks, "
                "cached_blocks = cached_blocks + excluded.cached_blocks",
                (day, site, cost, requests, input_tokens, blocks, cached_blocks))
            self.db.commit()

    def stats(self):
        today = datetime.date.today()
        since = lambda n: (today - datetime.timedelta(days=n - 1)).isoformat()
        cols = ("COALESCE(SUM(cost), 0), COALESCE(SUM(requests), 0), COALESCE(SUM(input_tokens), 0), "
                "COALESCE(SUM(blocks), 0), COALESCE(SUM(cached_blocks), 0)")
        keys = ("cost", "requests", "input_tokens", "blocks", "cached_blocks")
        with self.lock:
            totals = {}
            for name, cond, args in (("today", "day = ?", (today.isoformat(),)), ("d7", "day >= ?", (since(7),)),
                                     ("d30", "day >= ?", (since(30),)), ("all", "1 = 1", ())):
                totals[name] = dict(zip(keys, self.db.execute(f"SELECT {cols} FROM usage WHERE {cond}", args).fetchone()))
            rows = dict(self.db.execute("SELECT day, SUM(cost) FROM usage WHERE day >= ? GROUP BY day",
                                        (since(14),)).fetchall())
            sites = self.db.execute("SELECT site, SUM(cost), SUM(blocks) FROM usage WHERE day >= ? AND site != '' "
                                    "GROUP BY site HAVING SUM(cost) > 0 ORDER BY SUM(cost) DESC LIMIT 5",
                                    (since(30),)).fetchall()
        daily = [{"day": d, "cost": rows.get(d, 0.0)}
                 for d in ((today - datetime.timedelta(days=i)).isoformat() for i in range(13, -1, -1))]
        return {"totals": totals, "daily": daily,
                "sites": [{"site": st, "cost": c, "blocks": b} for st, c, b in sites]}


STORE = Store(CACHE_DIR / "cache.sqlite")


def block_key(text, model):
    return hashlib.sha256(f"{PROMPT_VERSION}\n{model}\n{text}".encode()).hexdigest()


# ---------- 段落分析：句子与候选词的字符位置 ----------

ANALYZE_LOCK = threading.Lock()   # jieba 不保证线程安全


def analyze(text):
    with ANALYZE_LOCK:
        return _analyze(text)


def _analyze(text):
    """返回 (lang, sentences)。sentences: [{"start", "end", "text", "units": [{"t", "kind", "start", "end"}]}]"""
    lang = reader.detect_lang(text)
    sentences = []
    for m in re.finditer(r"[^\n]+", text):          # 换行（<br> 或块边界）视为硬分隔
        seg, off = m.group(0), m.start()
        if lang == "zh":
            reader.add_custom_words(seg)
            pos = 0
            for s in reader.split_sentences(seg):
                i = seg.find(s, pos)
                if i < 0:
                    continue
                pos = i + len(s)
                sentences.append(_with_offsets(s, off + i, reader.build_units(s)))
        else:
            # 英文用标准库实现的规则方案（skimlight/english.py），不依赖 spaCy；
            # 在人工标注集上选词质量与 spaCy 方案相当（research/experiments/english_light.py）
            for start, s_text, units in english.sentences(seg):
                sentences.append(_with_offsets(s_text, off + start, units))
    return lang, sentences


def _with_offsets(s_text, start, units):
    pos = start
    for u in units:
        u["start"], u["end"] = pos, pos + len(u["t"])
        pos = u["end"]
    stripped = s_text.rstrip()
    return {"start": start, "end": start + len(stripped), "text": stripped.strip(), "units": units}


# ---------- Jev ----------

def post_jev(client, endpoint, body):
    for attempt in range(4):
        try:
            r = client.post(endpoint, json=body)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * 2 ** attempt)
                continue
            raise RuntimeError(f"Jev HTTP {r.status_code}: {r.text[:200]}")
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("Jev transport error: %s", e)
            time.sleep(1.5 * 2 ** attempt)
    raise RuntimeError("Jev request failed after retries")


def score_sentences(sents_by_lang, title, cfg):
    """按语言把句子打包成请求块（沿用 reader.py 的块大小与题目写法），并发请求 Jev，把概率写回 unit["p"]。"""
    bodies, refs = [], []
    for lang, sents in sents_by_lang.items():
        chunk, chars = [], 0
        chunks = []
        for s in sents:
            if chunk and chars + len(s["text"]) > reader.BLOCK_CHARS[lang]:
                chunks.append(chunk)
                chunk, chars = [], 0
            chunk.append(s)
            chars += len(s["text"])
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            for i, s in enumerate(chunk):
                s["id"] = f"s{i}"
            block = {"section": title or "", "paras": [{"sentences": chunk}]}
            for body, ref in reader.block_requests(block, "", lang):
                body["model"] = PROVIDERS[cfg["provider"]]["model"]
                bodies.append(body)
                refs.append(ref)
    if not bodies:
        return {"cost": 0.0, "input_tokens": 0, "requests": 0}
    endpoint = PROVIDERS[cfg["provider"]]["endpoint"]
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    with httpx.Client(headers=headers, timeout=120) as client, ThreadPoolExecutor(JEV_CONCURRENCY) as pool:
        results = list(pool.map(lambda b: post_jev(client, endpoint, b), bodies))
    usage = {"cost": 0.0, "input_tokens": 0, "requests": len(results)}
    for res, ref in zip(results, refs):
        tokens = res["usage"]["input_tokens"]
        usage["cost"] += res["usage"].get("cost") or tokens * PRICE_PER_INPUT_TOKEN
        usage["input_tokens"] += tokens
        for qid, ans in res["answers"].items():
            ref[qid]["p"] = round(ans["noul"], 3)
    return usage


# ---------- 请求处理 ----------

def handle_highlight(msg, cfg):
    blocks = msg.get("blocks", [])
    title = (msg.get("page") or {}).get("title", "")
    site = urlparse((msg.get("page") or {}).get("url", "")).hostname or ""
    out, todo = {}, []
    cached = 0
    model = PROVIDERS[cfg["provider"]]["model"]
    for b in blocks:
        text = b.get("text", "")
        if len(re.sub(r"\s", "", text)) < MIN_BLOCK_CHARS:
            out[b["id"]] = {"id": b["id"], "lang": None, "sentences": [], "cands": []}
            continue
        hit = STORE.get(block_key(text, model))
        if hit is not None:
            out[b["id"]] = {"id": b["id"], **hit}
            cached += 1
        else:
            todo.append(b)

    usage = {"cost": 0.0, "input_tokens": 0, "requests": 0}
    if todo:
        spent, _ = STORE.today()
        if spent >= cfg["daily_budget_usd"]:
            return {"error": f"已达到每日费用上限 ${cfg['daily_budget_usd']}（今日 ${spent:.4f}）",
                    "blocks": list(out.values())}
        if not cfg["api_key"]:
            return {"error": "未配置 API key：请在扩展的设置页中填写", "need_key": True, "blocks": list(out.values())}
        analyzed, by_lang = {}, {}
        for b in todo:
            lang, sents = analyze(b["text"])
            analyzed[b["id"]] = (b, lang, sents)
            by_lang.setdefault(lang, []).extend(sents)
        usage = score_sentences(by_lang, title, cfg)
        for bid, (b, lang, sents) in analyzed.items():
            cands = []
            for s in sents:
                for u in s["units"]:
                    if u["kind"] == "cand" and "p" in u:
                        cands.append([u["start"], u["end"], u["p"], 1])
                    elif u["kind"] == "neg":
                        cands.append([u["start"], u["end"], None, 2])
            result = {"lang": lang, "sentences": [[s["start"], s["end"]] for s in sents], "cands": cands}
            STORE.put(block_key(b["text"], model), result)
            out[bid] = {"id": bid, **result}

    STORE.add_usage(site, usage["cost"], usage["requests"], usage["input_tokens"], len(todo), cached)
    spent, n_req = STORE.today()
    usage.update({"cached_blocks": cached, "new_blocks": len(todo), "today_cost": spent, "today_requests": n_req})
    return {"blocks": [out[b["id"]] for b in blocks], "usage": usage}


# ---------- 协议 ----------

WRITE_LOCK = threading.Lock()


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    return json.loads(sys.stdin.buffer.read(n).decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    with WRITE_LOCK:
        sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
        sys.stdout.buffer.flush()


def dispatch(msg):
    t0 = time.time()
    try:
        cfg = load_config()
        if msg.get("type") == "ping":
            spent, n_req = STORE.today()
            resp = {"ok": True, "model": PROVIDERS[cfg["provider"]]["model"], "provider": cfg["provider"],
                    "today_cost": spent, "today_requests": n_req,
                    "budget": cfg["daily_budget_usd"], "has_key": bool(cfg["api_key"])}
        elif msg.get("type") == "get_config":
            resp = public_config(cfg)
        elif msg.get("type") == "set_config":
            resp = handle_set_config(msg)
        elif msg.get("type") == "test_key":
            resp = handle_test_key(msg)
        elif msg.get("type") == "stats":
            resp = {"ok": True, "budget": cfg["daily_budget_usd"], **STORE.stats()}
        elif msg.get("type") == "highlight":
            resp = handle_highlight(msg, cfg)
        else:
            resp = {"error": f"unknown message type {msg.get('type')}"}
    except Exception as e:  # 单条消息失败不影响后续消息
        log.exception("dispatch failed")
        resp = {"error": f"{type(e).__name__}: {e}"}
    resp["id"] = msg.get("id")
    if msg.get("type") == "highlight":
        u = resp.get("usage", {})
        log.info("highlight blocks=%d requests=%s cached=%s cost=%.6f %.2fs %s", len(msg.get("blocks", [])),
                 u.get("requests"), u.get("cached_blocks"), u.get("cost", 0), time.time() - t0,
                 resp.get("error", ""))
    send_message(resp)


def main():
    log.info("host started (pid %d)", os.getpid())
    with ThreadPoolExecutor(4) as pool:
        while True:
            msg = read_message()
            if msg is None:
                break
            pool.submit(dispatch, msg)
    log.info("host exiting")


if __name__ == "__main__":
    main()
