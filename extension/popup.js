const DEFAULTS = { th: 0.5, ratio: 0.3, dim: true, mark: false, neg: true, fullPage: false, autoSites: [] };
const $ = (id) => document.getElementById(id);
let S = { ...DEFAULTS };
let tab = null;

async function save() { await chrome.storage.sync.set({ settings: S }); }

// 费用金额很小，按量级选择小数位；非零但极小的值显示为 <$0.0001，避免显示成 $0.0000
function usd(v) {
  if (!v) return '$0';
  if (v < 0.0001) return '<$0.0001';
  if (v < 0.01) return `$${v.toFixed(4)}`;
  if (v < 1) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(2)}`;
}
const int = (n) => (n || 0).toLocaleString('zh-CN');

function showSettings() {
  $('th').value = S.th; $('thv').value = S.th.toFixed(2);
  $('ratio').value = S.ratio; $('ratiov').value = S.ratio.toFixed(2);
  $('dim').checked = S.dim; $('mark').checked = S.mark; $('neg').checked = S.neg; $('fullPage').checked = S.fullPage;
}

function showStatus(st) {
  const t = $('toggle');
  if (!st) {
    t.disabled = true; t.textContent = '此页面不支持';
    $('progress').textContent = '只支持 http / https 网页；刚安装扩展时需要刷新页面。';
    $('pageCost').textContent = '';
    return;
  }
  t.disabled = false;
  t.textContent = st.enabled ? '关闭本页' : '开启本页';
  t.classList.toggle('off', st.enabled);
  $('progress').textContent = st.enabled ? `正文 ${st.blocks} 段，已标注 ${st.done} 段${st.pending ? `，处理中 ${st.pending}` : ''}` : '';
  $('auto').checked = S.autoSites.includes(st.host);
  const p = st.page || {};
  $('pageCost').textContent = p.blocks
    ? `本页 ${usd(p.cost)} · ${int(p.blocks)} 段（其中缓存 ${int(p.cached)} 段）· ${int(p.requests)} 次请求`
    : '本页尚未产生费用';
}

function showStats(st) {
  if (!st?.ok) return;
  $('stats').hidden = false;
  const t = st.totals;
  $('todayCost').textContent = usd(t.today.cost);
  $('budget').textContent = `上限 ${usd(st.budget)}`;
  const ratio = st.budget ? Math.min(1, t.today.cost / st.budget) : 0;
  $('meter').querySelector('i').style.width = `${(ratio * 100).toFixed(1)}%`;
  $('meter').classList.toggle('warn', ratio >= 0.8);
  $('meter').setAttribute('aria-valuenow', (ratio * 100).toFixed(0));
  // blocks 为新处理的段数，cached_blocks 为命中缓存的段数
  const seen = t.today.blocks + t.today.cached_blocks;
  const hitRate = seen ? t.today.cached_blocks / seen : 0;
  $('todayDetail').textContent = `${int(t.today.requests)} 次请求 · ${int(t.today.input_tokens)} 输入 token` +
    (seen ? ` · 缓存命中 ${(hitRate * 100).toFixed(0)}%` : '');
  $('d7').textContent = usd(t.d7.cost);
  $('d30').textContent = usd(t.d30.cost);
  $('dall').textContent = usd(t.all.cost);

  // 近 14 天柱状图：单一系列，柱高按最大值归一；每列整列可悬停，提示日期与金额
  const chart = $('chart');
  chart.querySelectorAll('.col').forEach((c) => c.remove());
  const max = Math.max(...st.daily.map((d) => d.cost), 0);
  const tip = $('tip');
  for (const d of st.daily) {
    const col = document.createElement('div');
    col.className = 'col' + (d.cost ? '' : ' zero');
    const bar = document.createElement('i');
    bar.style.height = d.cost && max ? `${Math.max(4, (d.cost / max) * 100)}%` : '1px';
    col.appendChild(bar);
    const label = `${d.day.slice(5).replace('-', '/')} ${usd(d.cost)}`;
    col.setAttribute('aria-label', label);
    col.onmouseenter = () => {
      tip.textContent = label; tip.style.display = 'block';
      const x = col.offsetLeft + col.offsetWidth / 2;
      tip.style.left = `${Math.min(Math.max(x, 40), chart.offsetWidth - 40)}px`;
    };
    col.onmouseleave = () => { tip.style.display = 'none'; };
    chart.appendChild(col);
  }
  chart.setAttribute('aria-label', `近 14 天每日费用，最高 ${usd(max)}`);
  $('axisStart').textContent = st.daily[0].day.slice(5).replace('-', '/');
  $('axisMax').textContent = max ? `最高 ${usd(max)}` : '';

  $('sitesBox').hidden = !st.sites.length;
  $('sites').innerHTML = '';
  for (const s of st.sites) {
    const li = document.createElement('li');
    const name = document.createElement('span'); name.textContent = s.site;
    const val = document.createElement('span'); val.className = 'num'; val.textContent = usd(s.cost);
    li.append(name, val);
    $('sites').appendChild(li);
  }
}

async function tabMessage(msg) {
  try { return await chrome.tabs.sendMessage(tab.id, msg); } catch { return null; }
}

async function refreshStats() {
  const st = await chrome.runtime.sendMessage({ type: 'stats' });
  showStats(st);
}

async function init() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const { settings } = await chrome.storage.sync.get('settings');
  S = { ...DEFAULTS, ...settings };
  showSettings();
  const host = tab?.url ? new URL(tab.url).hostname : '';
  $('page').textContent = host;
  showStatus(await tabMessage({ type: 'status' }));

  $('toggle').onclick = async () => showStatus(await tabMessage({ type: 'toggle' }));
  $('auto').onchange = async (e) => {
    const set = new Set(S.autoSites);
    e.target.checked ? set.add(host) : set.delete(host);
    S.autoSites = [...set]; await save();
    if (e.target.checked) showStatus(await tabMessage({ type: 'enable' }));
  };
  $('th').oninput = (e) => { S.th = +e.target.value; $('thv').value = S.th.toFixed(2); save(); };
  $('ratio').oninput = (e) => { S.ratio = +e.target.value; $('ratiov').value = S.ratio.toFixed(2); save(); };
  for (const id of ['dim', 'mark', 'neg', 'fullPage']) $(id).onchange = (e) => { S[id] = e.target.checked; save(); };

  $('openOptions').onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
  $('setKey').onclick = () => chrome.runtime.openOptionsPage();
  const ping = await chrome.runtime.sendMessage({ type: 'ping' });
  const hostEl = $('host');
  if (ping?.ok) {
    hostEl.textContent = `本地程序已连接 · ${ping.provider === 'typesafe' ? 'TypeSafe' : 'OpenRouter'}`;
    $('setKey').hidden = ping.has_key;
    refreshStats();
  } else {
    hostEl.innerHTML = '<span class="err"></span><br>请先安装 Skimlight 本地程序，见设置页中的说明。';
    hostEl.querySelector('.err').textContent = `本地程序未连接：${ping?.error || ping?.lastError || '未知错误'}`;
  }
  let tick = 0;
  setInterval(async () => {
    const st = await tabMessage({ type: 'status' });
    if (st) showStatus(st);
    if (ping?.ok && ++tick % 3 === 0) refreshStats();   // 每 3 秒刷新一次统计
  }, 1000);
}

init();
