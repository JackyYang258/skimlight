"""用 Native Messaging 协议驱动本地程序做自测：python scripts/host_selftest.py [启动命令...]"""
import json, struct, subprocess, sys, time
cmd = sys.argv[1:] or [sys.executable, "host/skimlight_host.py"]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
def call(msg):
    data = json.dumps(msg, ensure_ascii=False).encode()
    p.stdin.write(struct.pack("<I", len(data)) + data); p.stdin.flush()
    n = struct.unpack("<I", p.stdout.read(4))[0]
    return json.loads(p.stdout.read(n))
t = time.time(); print("ping", call({"id": 1, "type": "ping"}), f"{time.time()-t:.2f}s")
blocks = [
    {"id": "b1", "text": "Rain fell minutes before the race, delaying the start, and as the track dried Massa established a lead of several seconds. More rain late in the race could not prevent Massa from winning."},
    {"id": "b2", "text": "于是到了7月28日，国务卿正式宣布修正案成为宪法的一部分，部分州对批准的撤消均不生效。\n平等保护条款要求各州对其管辖范围内的任何人以平等法律保护。"},
    {"id": "b3", "text": "短"},
]
for rnd in (1, 2):
    t = time.time(); r = call({"id": 10 + rnd, "type": "highlight", "page": {"title": "selftest"}, "blocks": blocks})
    print(f"round {rnd}: {time.time()-t:.2f}s usage={r.get('usage')} error={r.get('error')}")
for b in r["blocks"]:
    text = next(x["text"] for x in blocks if x["id"] == b["id"])
    for s, e in b["sentences"]:
        cs = [c for c in b["cands"] if s <= c[0] < e]
        top = sorted([c for c in cs if c[3] == 1], key=lambda c: -c[2])[:3] + [c for c in cs if c[3] == 2]
        mark = "".join(f"【{ch}" if any(c[0] == i for c in top) else ch for i, ch in enumerate(text[s:e], s))
        print(f"  {b['id']} {b['lang']}: {text[s:e]!r}\n     top: {[(text[c[0]:c[1]], c[2]) for c in top]}")
p.stdin.close(); p.wait(timeout=10)
