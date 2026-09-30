// 内容脚本：定位正文 → 提取段落文本（保留到 DOM 的逐字映射）→ 只处理可视区域附近的段落
// → 用 CSS Custom Highlight API 标出关键词（不插入元素，关闭后页面完全恢复）。
(() => {
  if (window.__skimlight) return;
  window.__skimlight = true;

  const KMAX = 4;
  const MSG_CHARS = 12000;           // 每条消息的正文上限
  const MAX_RETRIES = 3;             // 请求失败的段落最多重试次数（间隔 2s、4s、6s）
  const MIN_CHARS = 20;              // 去掉空白后短于该长度的段落不处理
  const SPLIT_CHARS = 2500;          // 超过该长度的块拆成若干小段，按各自位置分别处理
  const PART_CHARS = 1500;
  const EXCLUDE = [
    'script', 'style', 'noscript', 'template', 'textarea', 'input', 'select', 'button', 'svg', 'math', 'canvas',
    'pre', 'code', 'kbd', 'samp', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'nav', 'header', 'footer', 'aside', 'form', '[role=navigation]', '[role=complementary]', '[aria-hidden=true]',
    '[contenteditable=""]', '[contenteditable=true]', '.katex', '.MathJax', 'mjx-container', '.skim-toast-host',
  ].join(',');
  const ROOT_SELECTORS = [
    'article', 'main', '[role=main]', '.post', '.post-content', '.entry-content', '.article-content',
    '.article', '.markdown-body', '.RichText', '#content', '.content', '#mw-content-text',
  ].join(',');
  const INLINE = new Set(['inline', 'inline-block', 'inline-flex', 'inline-grid', 'contents', 'ruby']);

  const DEFAULTS = { th: 0.5, ratio: 0.3, dim: true, mark: false, neg: true, fullPage: false, autoSites: [] };
  let S = { ...DEFAULTS };
  let enabled = false;
  let root = null;
  let io = null, mo = null, rescanTimer = null, flushTimer = null;
  const blocks = new Map();          // 元素 → 段落对象数组（长块拆成多段，共用同一元素）
  const bigEls = new Set();          // 进入可视区域、需要按小段判断位置的长块元素
  const allParts = function* () { for (const ps of blocks.values()) yield* ps; };
  const queue = new Set();           // 待请求的段落
  let inflight = 0;
  const pageStats = { cost: 0, requests: 0, blocks: 0, cached: 0 };   // 本页累计费用
  let nextId = 1;
  let keyColor = null;
  const hlKey = new Highlight();
  const hlNeg = new Highlight();

  // ---------- 正文定位 ----------

  function textLen(el) { return (el.innerText || '').replace(/\s+/g, '').length; }

  function pickRoot() {
    const cands = [...document.querySelectorAll(ROOT_SELECTORS)]
      .map((el) => [el, textLen(el)]).filter(([, n]) => n >= 300).sort((a, b) => b[1] - a[1]);
    if (cands.length) {
      let [best, bestLen] = cands[0];
      // 选更具体的容器：被包含且保留了大部分正文的候选
      for (const [el, n] of cands) if (el !== best && best.contains(el) && n >= 0.7 * bestLen) [best, bestLen] = [el, n];
      return best;
    }
    const score = new Map();
    for (const p of document.querySelectorAll('p')) {
      const n = textLen(p);
      if (n >= 40 && p.parentElement) score.set(p.parentElement, (score.get(p.parentElement) || 0) + n);
    }
    let best = null, bestN = 0;
    for (const [el, n] of score) if (n > bestN) [best, bestN] = [el, n];
    return best && bestN >= 300 ? best : document.body;
  }

  // ---------- 段落提取：纯文本 + 逐字映射 ----------

  const displayCache = new WeakMap();
  function isInline(el) {
    let d = displayCache.get(el);
    if (d === undefined) { d = getComputedStyle(el).display; displayCache.set(el, d); }
    return INLINE.has(d);
  }
  function blockOf(el) {
    while (el && el !== root && isInline(el)) el = el.parentElement;
    return el || root;
  }

  function scan() {
    const groups = new Map();        // 块元素 → [文本节点或 'BR']
    const order = [];
    let lastBlock = null;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (n.nodeType === 1) {
          if (n.matches(EXCLUDE)) return NodeFilter.FILTER_REJECT;
          return n.tagName === 'BR' ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const blk = blockOf(n.nodeType === 1 ? n.parentElement : n.parentElement);
      if (!groups.has(blk)) { groups.set(blk, []); order.push(blk); }
      const g = groups.get(blk);
      if (n.nodeType === 1) g.push('BR');
      else {
        if (lastBlock && lastBlock !== blk && g.length) g.push('BR');   // 中间隔了别的块
        g.push(n);
      }
      lastBlock = blk;
    }
    const found = [];
    for (const el of order) {
      const b = buildBlock(el, groups.get(el));
      if (!b) continue;
      const old = blocks.get(el);
      const oldText = old ? old.map((x) => x.text).join('') : null;
      const parts = splitBlock(b);
      if (old && oldText === parts.map((x) => x.text).join('')) {
        old.forEach((o, i) => { o.nodes = parts[i].nodes; o.map = parts[i].map; });
        found.push(...old); continue;
      }
      if (old) { old.forEach(clearBlock); el.classList.remove('skim-dim'); }
      parts.forEach((x) => { x.id = `b${nextId++}`; });
      blocks.set(el, parts);
      found.push(...parts);
      io?.observe(el);
    }
    return found;
  }

  function buildBlock(el, items) {
    if (el.checkVisibility && !el.checkVisibility()) return null;
    const nodes = [];
    const chars = [];
    const mapNode = [];
    const mapOff = [];
    let pendingSpace = false;
    const push = (ch, ni, off) => { chars.push(ch); mapNode.push(ni); mapOff.push(off); };
    for (const it of items) {
      if (it === 'BR') {
        if (chars.length && chars[chars.length - 1] !== '\n') { while (chars[chars.length - 1] === ' ') { chars.pop(); mapNode.pop(); mapOff.pop(); } push('\n', -1, 0); }
        pendingSpace = false;
        continue;
      }
      const ni = nodes.push(it) - 1;
      const s = it.nodeValue;
      for (let i = 0; i < s.length; i++) {
        const c = s[i];
        if (/\s/.test(c)) { pendingSpace = true; continue; }
        if (pendingSpace && chars.length && chars[chars.length - 1] !== '\n') push(' ', ni, i);
        pendingSpace = false;
        push(c, ni, i);
      }
    }
    const text = chars.join('').replace(/\s+$/, '');
    if (text.replace(/\s/g, '').length < MIN_CHARS) return null;
    return { el, text, nodes, map: { node: mapNode, off: mapOff }, res: null, ranges: [], requested: false };
  }

  function splitBlock(b) {
    if (b.text.length <= SPLIT_CHARS) return [b];
    // 优先在换行（<br>）处切分，其次在句末标点处
    const parts = [];
    let start = 0;
    while (start < b.text.length) {
      let end = Math.min(b.text.length, start + PART_CHARS);
      if (end < b.text.length) {
        const win = b.text.slice(start + PART_CHARS * 0.5, end + 500);
        let cut = win.lastIndexOf('\n');
        if (cut < 0) { const m = [...win.matchAll(/[。！？.!?]\s/g)].pop(); cut = m ? m.index + 1 : -1; }
        end = cut >= 0 ? start + PART_CHARS * 0.5 + cut + 1 : end;
        end = Math.round(end);
      }
      parts.push({ ...b, text: b.text.slice(start, end),
                   map: { node: b.map.node.slice(start, end), off: b.map.off.slice(start, end) },
                   res: null, ranges: [], requested: false });
      start = end;
    }
    return parts.filter((x) => x.text.replace(/\s/g, '').length >= MIN_CHARS);
  }

  function checkBigParts() {
    const lo = -window.innerHeight, hi = window.innerHeight * 2.5;
    for (const el of bigEls) {
      for (const b of blocks.get(el) || []) {
        if (b.res || b.requested) continue;
        const r = makeRange(b, 0, Math.min(b.text.length, 2));
        const rect = r?.getBoundingClientRect();
        if (rect && rect.bottom > lo && rect.top < hi) enqueue(b);
      }
    }
  }
  let scrollTimer = null;
  const onScroll = () => { if (!scrollTimer) scrollTimer = setTimeout(() => { scrollTimer = null; checkBigParts(); }, 200); };

  // ---------- 请求 ----------

  function enqueue(b) {
    if (b.res || b.requested) return;
    queue.add(b);
    clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, 120);
  }

  async function flush() {
    if (!enabled || !queue.size) return;
    const batch = [];
    let n = 0;
    for (const b of queue) {
      if (batch.length && n + b.text.length > MSG_CHARS) break;
      batch.push(b); n += b.text.length;
    }
    batch.forEach((b) => { queue.delete(b); b.requested = true; });
    inflight++;
    toast(`Skimlight：处理中（${batch.length} 段）…`);
    let resp;
    try {
      resp = await chrome.runtime.sendMessage({
        type: 'highlight', page: { title: document.title, url: location.href },
        blocks: batch.map((b) => ({ id: b.id, text: b.text })),
      });
    } catch (e) { resp = { error: String(e) }; }
    inflight--;
    if (resp?.usage) {
      pageStats.cost += resp.usage.cost || 0;
      pageStats.requests += resp.usage.requests || 0;
      pageStats.blocks += (resp.usage.new_blocks || 0) + (resp.usage.cached_blocks || 0);
      pageStats.cached += resp.usage.cached_blocks || 0;
    }
    const byId = new Map((resp?.blocks || []).map((r) => [r.id, r]));
    let failed = 0;
    for (const b of batch) {
      const r = byId.get(b.id);
      if (r) { b.res = r; if (enabled) renderBlock(b); continue; }
      b.requested = false;
      b.retries = (b.retries || 0) + 1;
      if (b.retries <= MAX_RETRIES && !resp?.need_key) { failed++; setTimeout(() => enabled && enqueue(b), 2000 * b.retries); }
    }
    if (resp?.need_key) chrome.runtime.sendMessage({ type: 'need_key' }).catch(() => {});
    if (resp?.error) toast(`Skimlight：${resp.error}${failed && !resp.need_key ? `（${failed} 段稍后重试）` : ''}`, 8000);
    else if (!inflight && !queue.size) toast(statusText(), 1500);
    if (queue.size) flush();
  }

  // ---------- 选词与渲染 ----------

  function pick(b) {
    const out = [];
    const { sentences, cands } = b.res;
    for (const [ss, se] of sentences) {
      const inS = cands.filter((c) => c[0] >= ss && c[0] < se);
      const cs = inS.filter((c) => c[3] === 1);
      const k = cs.length ? Math.min(KMAX, Math.max(1, Math.round(cs.length * S.ratio))) : 0;
      const pool = cs.filter((c) => c[2] >= S.th).sort((a, b2) => b2[2] - a[2]);
      const seen = new Set();
      for (const c of pool) {
        if (seen.size >= k) break;
        const t = b.text.slice(c[0], c[1]);
        if (seen.has(t)) continue;
        seen.add(t); out.push([c[0], c[1], 1]);
      }
      if (S.neg) for (const c of inS) if (c[3] === 2) out.push([c[0], c[1], 2]);
    }
    return out;
  }

  function makeRange(b, s, e) {
    const { node, off } = b.map;
    while (s < e && node[s] < 0) s++;
    while (e > s && node[e - 1] < 0) e--;
    if (s >= e) return null;
    const n1 = b.nodes[node[s]], n2 = b.nodes[node[e - 1]];
    if (!n1?.isConnected || !n2?.isConnected) return null;
    const r = new Range();
    r.setStart(n1, off[s]);
    r.setEnd(n2, off[e - 1] + 1);
    return r;
  }

  function clearBlock(b) {
    for (const r of b.ranges) { hlKey.delete(r); hlNeg.delete(r); }
    b.ranges = [];
  }

  function renderBlock(b) {
    clearBlock(b);
    if (!b.res || !b.res.cands.length) return;
    if (!keyColor) { keyColor = getComputedStyle(b.el).color; updateStyle(); }
    for (const [s, e, kind] of pick(b)) {
      const r = makeRange(b, s, e);
      if (!r) continue;
      (kind === 2 ? hlNeg : hlKey).add(r);
      b.ranges.push(r);
    }
    b.el.classList.toggle('skim-dim', S.dim);
  }

  function renderAll() { for (const b of allParts()) if (b.res) renderBlock(b); }

  function updateStyle() {
    let st = document.getElementById('skim-style');
    if (!st) { st = document.createElement('style'); st.id = 'skim-style'; document.documentElement.appendChild(st); }
    const c = keyColor || 'CanvasText';
    st.textContent = `
      .skim-dim { color: color-mix(in srgb, currentColor 55%, transparent) !important; }
      ::highlight(skim-key) { color: ${c}; text-shadow: 0.35px 0 0 ${c}; ${S.mark ? 'background-color: rgba(255, 200, 60, .35);' : ''} }
      ::highlight(skim-neg) { color: #c0392b; text-shadow: 0.35px 0 0 #c0392b; }`;
  }

  // ---------- 开关 ----------

  function enable() {
    if (enabled) return;
    enabled = true;
    root = pickRoot();
    CSS.highlights.set('skim-key', hlKey);
    CSS.highlights.set('skim-neg', hlNeg);
    updateStyle();
    io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        const ps = blocks.get(en.target);
        if (!ps) continue;
        if (ps.length === 1) { if (en.isIntersecting) enqueue(ps[0]); continue; }
        en.isIntersecting ? bigEls.add(en.target) : bigEls.delete(en.target);
        checkBigParts();
      }
    }, { rootMargin: '0px 0px 150% 0px' });
    const found = scan();
    renderAll();
    if (S.fullPage) found.forEach(enqueue);
    mo = new MutationObserver(() => {
      clearTimeout(rescanTimer);
      rescanTimer = setTimeout(() => { if (enabled) { const f = scan(); if (S.fullPage) f.forEach(enqueue); } }, 600);
    });
    mo.observe(root, { childList: true, subtree: true, characterData: true });
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll, { passive: true });
    chrome.runtime.sendMessage({ type: 'state', enabled: true }).catch(() => {});
    toast(`Skimlight：已开启，正文 ${found.length} 段`, 1500);
  }

  function disable() {
    if (!enabled) return;
    enabled = false;
    io?.disconnect(); mo?.disconnect(); io = mo = null;
    window.removeEventListener('scroll', onScroll);
    window.removeEventListener('resize', onScroll);
    bigEls.clear();
    queue.clear();
    for (const b of allParts()) { clearBlock(b); b.requested = !!b.res; b.el.classList.remove('skim-dim'); }
    CSS.highlights.delete('skim-key');
    CSS.highlights.delete('skim-neg');
    document.getElementById('skim-style')?.remove();
    chrome.runtime.sendMessage({ type: 'state', enabled: false }).catch(() => {});
    toast('Skimlight：已关闭', 1200);
  }

  function statusText() {
    let done = 0, total = 0;
    for (const b of allParts()) { total++; if (b.res) done++; }
    return `Skimlight：已标注 ${done} / ${total} 段` + (done < total && !S.fullPage ? '，滚动后继续处理' : '');
  }

  // ---------- 提示条（Shadow DOM，不受页面样式影响） ----------

  let toastEl = null, toastTimer = null;
  function toast(text, ms = 0) {
    if (!toastEl) {
      const host = document.createElement('div');
      host.className = 'skim-toast-host';
      host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;';
      const sh = host.attachShadow({ mode: 'open' });
      sh.innerHTML = `<div style="font:12px/1.4 system-ui,sans-serif;background:rgba(30,30,34,.88);color:#fff;
        padding:6px 10px;border-radius:6px;max-width:360px;box-shadow:0 2px 8px rgba(0,0,0,.2)"></div>`;
      document.documentElement.appendChild(host);
      toastEl = { host, box: sh.firstElementChild };
    }
    toastEl.box.textContent = text;
    toastEl.host.style.display = 'block';
    clearTimeout(toastTimer);
    if (ms) toastTimer = setTimeout(() => { toastEl.host.style.display = 'none'; }, ms);
  }

  // ---------- 消息与设置 ----------

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'toggle') enabled ? disable() : enable();
    else if (msg.type === 'enable') enable();
    else if (msg.type === 'disable') disable();
    if (['toggle', 'enable', 'disable', 'status'].includes(msg.type)) {
      let done = 0, total = 0; for (const b of allParts()) { total++; if (b.res) done++; }
      sendResponse({ enabled, blocks: total, done, pending: queue.size + inflight, host: location.hostname, page: pageStats });
    }
    return false;
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'sync' || !changes.settings) return;
    const wasFull = S.fullPage;
    S = { ...DEFAULTS, ...changes.settings.newValue };
    if (enabled) {
      updateStyle(); renderAll();
      if (S.fullPage && !wasFull) for (const b of allParts()) enqueue(b);
    }
  });

  chrome.storage.sync.get('settings').then(({ settings }) => {
    S = { ...DEFAULTS, ...settings };
    if (S.autoSites.includes(location.hostname)) enable();
  });

  // 供自动化测试使用，只在本机页面上生效，避免任意网页触发请求
  if (['localhost', '127.0.0.1'].includes(location.hostname)) window.addEventListener('skim-test', (e) => {
    if (e.detail === 'enable') enable();
    if (e.detail === 'disable') disable();
  });
})();
