const $ = (id) => document.getElementById(id);
const KEY_PAGES = {
  openrouter: ['OpenRouter', 'https://openrouter.ai/keys', '经 OpenRouter 调用 Jev（typesafe/jev-1.13），key 以 sk-or- 开头'],
  typesafe: ['TypeSafe 控制台', 'https://console.typesafe.ai', '直接调用 TypeSafe 官方 API（jev-latest）'],
};
const INSTALL_URL = 'https://github.com/JackyYang258/skimlight#install';
let cfg = null;
// 把 Chrome 的 Native Messaging 错误翻译成可操作的建议
function hostErrorHint(err = '') {
  if (/not found/i.test(err)) return '找不到本地程序的注册信息：请重新运行 install.cmd（Windows 安装包）或 scripts/install_windows.sh（WSL）。';
  if (/forbidden/i.test(err)) return '本地程序不允许此扩展连接：请重新运行 install.cmd，并从安装目录中的 extension 文件夹加载扩展。';
  if (/exited|disconnected|断开/i.test(err)) return '本地程序启动后退出：请重新运行 install.cmd；如仍失败，查看 %LOCALAPPDATA%\\Skimlight\\data\\host.log。';
  return '请先安装 Skimlight 本地程序。';
}


const send = (msg) => chrome.runtime.sendMessage(msg).catch((e) => ({ error: String(e) }));

function setMsg(text, cls = '') { $('msg').className = cls; $('msg').textContent = text; }

function selectedProvider() { return document.querySelector('input[name=provider]:checked')?.value || 'openrouter'; }

function showProviderHint() {
  const [name, url, desc] = KEY_PAGES[selectedProvider()];
  $('providerHint').innerHTML = '';
  $('providerHint').append(`${desc}。获取 key：`);
  const a = document.createElement('a'); a.href = url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = name;
  $('providerHint').append(a);
}

function showKeyHint() {
  const same = cfg && selectedProvider() === cfg.provider;
  $('key').placeholder = cfg?.has_key && same ? `已保存（末 4 位 ${cfg.key_hint}），留空则不修改` : '粘贴 API key';
  $('keyHint').textContent = cfg?.config_path ? `保存位置：${cfg.config_path}` : '';
}

function render() {
  $('providers').innerHTML = '';
  for (const [id, name] of Object.entries(cfg.providers)) {
    const label = document.createElement('label');
    const r = document.createElement('input');
    r.type = 'radio'; r.name = 'provider'; r.value = id; r.checked = id === cfg.provider;
    r.onchange = () => { showProviderHint(); showKeyHint(); };
    label.append(r, id === 'openrouter' ? `${name}（推荐）` : `${name} 官方`);
    $('providers').appendChild(label);
  }
  $('budget').value = cfg.daily_budget_usd;
  showProviderHint();
  showKeyHint();
}

async function init() {
  const res = await send({ type: 'get_config' });
  if (!res?.ok) {
    $('hostDot').className = 'dot off';
    $('hostStatus').textContent = '未连接到本地程序';
    $('hostHint').innerHTML = '';
    $('hostHint').append(`${res?.error || ''} ${hostErrorHint(res?.error)} 安装说明：`);
    const a = document.createElement('a'); a.href = INSTALL_URL; a.target = '_blank'; a.textContent = '安装说明';
    $('hostHint').append(a);
    $('form').querySelectorAll('input, button').forEach((el) => { el.disabled = true; });
    return;
  }
  cfg = res;
  $('hostDot').className = 'dot on';
  $('hostStatus').textContent = cfg.has_key ? '已连接，API key 已配置' : '已连接，尚未配置 API key';
  render();
}

$('reveal').onclick = () => {
  const show = $('key').type === 'password';
  $('key').type = show ? 'text' : 'password';
  $('reveal').textContent = show ? '隐藏' : '显示';
};

$('save').onclick = async () => {
  const budget = parseFloat($('budget').value);
  if (!(budget > 0 && budget <= 100)) { setMsg('每日费用上限需在 0 到 100 美元之间', 'err'); return; }
  const provider = selectedProvider();
  const key = $('key').value.trim();
  if (!key && !(cfg.has_key && provider === cfg.provider)) { setMsg('请填写 API key', 'err'); return; }
  $('save').disabled = true; setMsg('正在保存…');
  const res = await send({ type: 'set_config', provider, api_key: key || undefined, daily_budget_usd: budget });
  $('save').disabled = false;
  if (!res?.ok) { setMsg(`保存失败：${res?.error || '未知错误'}`, 'err'); return; }
  cfg = res; $('key').value = ''; render();
  $('hostStatus').textContent = '已连接，API key 已配置';
  setMsg('已保存。可以点「测试连接」确认 key 是否可用。', 'ok');
};

$('test').onclick = async () => {
  const provider = selectedProvider();
  const key = $('key').value.trim();
  $('test').disabled = true; setMsg('正在测试（发送一道题的请求，费用约 $0.00001）…');
  const res = await send({ type: 'test_key', provider, api_key: key || undefined });
  $('test').disabled = false;
  if (res?.ok) setMsg(`连接成功：${res.model}，耗时 ${res.latency_ms} ms${key ? '。别忘了点「保存」。' : ''}`, 'ok');
  else setMsg(`测试失败：${res?.error || '未知错误'}`, 'err');
};

init();
