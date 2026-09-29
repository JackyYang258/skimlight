"""端到端测试：Playwright Chromium 加载扩展 + 本地程序，打开页面，开启标注，检查高亮与关闭后的恢复。
用法：python scripts/e2e_test.py [URL ...]   不带 URL 时使用 tests/pages/blog.html"""
import functools, http.server, json, os, sys, tempfile, threading, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
EXT_ID = (ROOT / "extension_id.txt").read_text().strip()
# 每次运行使用新的浏览器配置目录：复用旧目录时，Chromium 可能继续运行缓存的旧版后台脚本
PROFILE = Path(os.environ.get("SKIM_TEST_PROFILE") or tempfile.mkdtemp(prefix="skimlight-e2e-"))
SHOTS = Path(os.environ.get("SKIM_TEST_SHOTS", str(ROOT / "tests" / "shots")))

def register_host():
    manifest = {"name": "com.skimlight.host", "description": "Skimlight local host", "type": "stdio",
                "path": str(ROOT / "host" / "skimlight_host.sh"), "allowed_origins": [f"chrome-extension://{EXT_ID}/"]}
    for d in (PROFILE / "NativeMessagingHosts", Path.home() / ".config" / "chromium" / "NativeMessagingHosts"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "com.skimlight.host.json").write_text(json.dumps(manifest, indent=2))

def serve():
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "tests" / "pages"))
    h.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]

HL_JS = """() => {
  const out = {};
  for (const name of ['skim-key', 'skim-neg']) {
    const h = CSS.highlights.get(name);
    out[name] = h ? [...h].map(r => r.toString()) : null;
  }
  out.dim = document.querySelectorAll('.skim-dim').length;
  return out;
}"""

def run(urls):
    register_host()
    SHOTS.mkdir(parents=True, exist_ok=True)
    port = serve()
    urls = urls or [f"http://127.0.0.1:{port}/blog.html"]
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), channel="chromium", headless=True,
            viewport={"width": 1100, "height": 1400},
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        assert EXT_ID in sw.url, f"extension id mismatch: {sw.url}"
        for i, url in enumerate(urls):
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            t0 = time.time()
            if "127.0.0.1" in url:
                page.evaluate("window.dispatchEvent(new CustomEvent('skim-test', {detail: 'enable'}))")
            else:   # 模拟点击工具栏：通过后台向该标签页发 toggle
                sw.evaluate("""async (u) => { const [t] = await chrome.tabs.query({url: u + '*'});
                               return chrome.tabs.sendMessage(t.id, {type: 'enable'}); }""", url.split('#')[0])
            hl = None
            for _ in range(60):
                page.wait_for_timeout(500)
                hl = page.evaluate(HL_JS)
                if hl["skim-key"]:
                    page.wait_for_timeout(1500); hl = page.evaluate(HL_JS); break
            print(f"\n=== {url}\n首批高亮出现 {time.time()-t0:.1f}s；关键词 {len(hl['skim-key'] or [])} 个，否定词 {len(hl['skim-neg'] or [])} 个，调浅段落 {hl['dim']}")
            print("关键词示例:", (hl["skim-key"] or [])[:40])
            print("否定词:", hl["skim-neg"])
            page.screenshot(path=str(SHOTS / f"page{i}_on.png"))
            if os.environ.get("SKIM_TEST_SCROLL"):   # 滚动到页面中部，检查后续段落是否按需处理
                n0 = len(hl["skim-key"] or [])
                page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.5)")
                page.wait_for_timeout(5000)
                n1 = len(page.evaluate(HL_JS)["skim-key"] or [])
                print(f"滚动到中部后：关键词 {n0} → {n1}")
                page.screenshot(path=str(SHOTS / f"page{i}_scrolled.png"))
            page.evaluate("window.dispatchEvent(new CustomEvent('skim-test', {detail: 'disable'}))") if "127.0.0.1" in url else \
                sw.evaluate("""async (u) => { const [t] = await chrome.tabs.query({url: u + '*'});
                               return chrome.tabs.sendMessage(t.id, {type: 'disable'}); }""", url.split('#')[0])
            page.wait_for_timeout(500)
            off = page.evaluate(HL_JS)
            print("关闭后:", off, "样式残留:", page.evaluate("!!document.getElementById('skim-style')"))
        stats = sw.evaluate("chrome.storage.local.get('stats')")
        print("\n扩展统计:", stats)
        ctx.close()

if __name__ == "__main__":
    run(sys.argv[1:])
