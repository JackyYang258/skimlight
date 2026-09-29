// 后台：与本地程序（Native Messaging）保持一个长连接，转发内容脚本的请求；处理快捷键与图标状态。
const HOST = 'com.skimlight.host';

let port = null;
let seq = 0;
const pending = new Map();   // 请求 id → 回调
let lastError = '';

function ensurePort() {
  if (port) return port;
  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener((msg) => {
    const cb = pending.get(msg.id);
    if (cb) { pending.delete(msg.id); cb(msg); }
  });
  port.onDisconnect.addListener(() => {
    lastError = chrome.runtime.lastError?.message || '本地程序已断开';
    for (const cb of pending.values()) cb({ error: lastError });
    pending.clear();
    port = null;
  });
  return port;
}

function callHost(msg) {
  return new Promise((resolve) => {
    const id = ++seq;
    pending.set(id, resolve);
    try {
      ensurePort().postMessage({ ...msg, id });
    } catch (e) {
      pending.delete(id);
      resolve({ error: String(e) });
    }
  });
}

async function recordUsage(usage) {
  if (!usage) return;
  const day = new Date().toISOString().slice(0, 10);
  const { stats = {} } = await chrome.storage.local.get('stats');
  const s = stats.day === day ? stats : { day, cost: 0, requests: 0, cachedBlocks: 0 };
  s.cost += usage.cost || 0;
  s.requests += usage.requests || 0;
  s.cachedBlocks += usage.cached_blocks || 0;
  s.todayCostHost = usage.today_cost;   // 本地程序记录的当日总费用（含其他浏览器窗口）
  await chrome.storage.local.set({ stats: s });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'highlight') {
    callHost({ type: 'highlight', page: msg.page, blocks: msg.blocks }).then((resp) => {
      recordUsage(resp.usage);
      sendResponse(resp);
    });
    return true;   // 异步回复
  }
  if (msg.type === 'stats') {
    callHost({ type: 'stats' }).then(sendResponse);
    return true;
  }
  if (msg.type === 'ping') {
    callHost({ type: 'ping' }).then((resp) => sendResponse({ ...resp, lastError }));
    return true;
  }
  if (msg.type === 'state' && sender.tab) {
    chrome.action.setBadgeText({ tabId: sender.tab.id, text: msg.enabled ? 'ON' : '' });
    chrome.action.setBadgeBackgroundColor({ tabId: sender.tab.id, color: '#2f5bd3' });
  }
  return false;
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== 'toggle-jev') return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) chrome.tabs.sendMessage(tab.id, { type: 'toggle' }).catch(() => {});
});
