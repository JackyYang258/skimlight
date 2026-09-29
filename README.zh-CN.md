# Skimlight

[English](README.md)

Skimlight 是一个 Chrome 扩展，在网页正文中标出每句的关键词并调浅其余文字，用于快速阅读 blog 和论文。支持中文和英文。按 **Alt+J** 开关当前页面。

![Skimlight 在测试页面上的效果](docs/screenshot.png)

选词由 TypeSafe AI 的判断模型 [Jev](https://typesafe.ai) 完成，通过 [OpenRouter](https://openrouter.ai) 调用。Jev 不生成文本，只对每个候选词给出"是否关键"的概率。本项目与 TypeSafe AI、OpenRouter 均无关联。

> **状态：早期原型。** 目前只提供 Windows + WSL2 + Chrome 的安装方式。不依赖 WSL 的 Windows 安装包、在界面里填写 API key、macOS / Linux 支持列在后面的计划中。

## 工作原理

1. **提取正文（页面内）**：定位正文容器，提取段落文字，同时记录每个字符对应的 DOM 文本节点。跳过导航栏、页眉页脚、代码块和公式。
2. **只处理读到的部分**：只把可视区域及其下方约 1.5 屏内的段落送去处理。超长的段落（例如用 `<br>` 分段的整篇文章）会拆成小段，出现在屏幕附近时才处理。
3. **生成候选词（本地程序）**：通过 Chrome Native Messaging 连接的 Python 本地程序负责分句、分词（中文用 jieba，英文用 spaCy），然后生成候选词：名词短语、实义动词、形容词和数字。否定词（`don't know`、`不生效`）按规则始终标出。
4. **Jev 判断**：每个候选词一道 Noul 题（`Key word in s3: "lead"?`）。判断标准在请求的 state 中只写一次，每题约 18 个输入 token。多个段落合并成一个请求，英文每块约 2000 字符，中文约 1200 字。
5. **显示（页面内）**：每句取 *p* 不低于阈值、概率最高的 *k* 个词（*k* 约为候选词数的 30%，1 到 4 个）。用 [CSS Custom Highlight API](https://developer.mozilla.org/zh-CN/docs/Web/API/CSS_Custom_Highlight_API) 显示，不插入任何页面元素，关闭后页面完全恢复。

结果按段落文本的哈希缓存在本地，重读同一页面不产生费用。本地程序还设有每日费用上限。

## 费用

Jev 在 OpenRouter 上的价格为每百万输入 token $0.042，输出免费。实测：

| 内容 | 费用 |
|---|---|
| 269 词的英文文章（全文） | $0.00012 |
| 1.46 万字的中文文章（全文） | $0.0036 |
| 长文章在屏幕附近的部分 | 约 $0.0007 |

由于只处理滚动到的段落，实际费用通常低于全文费用。

## 环境要求

- Windows 10/11，已安装 WSL2（目前本地程序在 WSL 中运行）
- Chrome 111 或更高版本
- WSL 中的 Python 3.10 或更高版本
- 一个 [OpenRouter](https://openrouter.ai) API key

## 安装（Windows + WSL）

在 WSL 中运行：

```bash
git clone https://github.com/JackyYang258/skimlight.git
cd skimlight
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p ~/.config/skimlight
cat > ~/.config/skimlight/config.json <<'EOF'
{ "api_key": "sk-or-...", "daily_budget_usd": 0.5 }
EOF
chmod 600 ~/.config/skimlight/config.json

bash scripts/install_windows.sh
```

安装脚本会把扩展复制到 `%LOCALAPPDATA%\Skimlight\extension`，并在注册表的 `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.skimlight.host` 下登记本地程序。

然后在 Chrome 中：

1. 打开 `chrome://extensions`，打开右上角的「开发者模式」。
2. 点「加载已解压的扩展程序」，选择 `%LOCALAPPDATA%\Skimlight\extension`。
3. 刷新已经打开的网页。

卸载：运行 `bash scripts/uninstall_windows.sh`，再到 `chrome://extensions` 移除扩展。

## 使用

- **开关**：按 **Alt+J** 或点击工具栏按钮，开关当前页面。快捷键可以在 `chrome://extensions/shortcuts` 修改。
- **弹出面板**提供以下功能：
  - **阈值**和**密度**滑块，调整后立即生效，不重新请求。
  - 「在此网站自动开启」。
  - 「一次处理整页」：默认关闭，此时只处理屏幕附近的段落，滚动时继续处理后面的。打开后，开启标注时整页一次处理完，费用更高。
  - 请求失败的段落会自动重试，最多 3 次。
  - 显示选项：调浅其余文字、关键词加底色、标出否定词。
  - 费用统计：本页费用；今日费用及其与每日上限的对比（进度条）；近 7 天、近 30 天和累计费用；近 14 天每日费用柱状图；近 30 天花费最多的网站。

## 数据发送范围

在某个页面开启 Skimlight 后，可视范围内段落的文字会先发给本地程序，再经 OpenRouter 发给 TypeSafe 的 Jev。关闭时不发送任何内容。本地缓存只保存字符位置和概率，以文本的哈希为键，不保存文本本身。

## 选词质量

在 24 个人工标注的句子（英文 14 句、中文 10 句）上计算 precision@k，即选出的词中被标注为关键词的比例：

| 方法 | 英文 | 中文 | 每题输入 token |
|---|---|---|---|
| Skimlight（精简题目写法） | 94% | 90% | 18–19 |
| 每题都写完整说明和判断标准 | 83% | 94% | 142–192 |
| TF-IDF 基线 | 49% | 49% | – |

标注集很小，而且只由一人标注，重复运行会有约 3–4 个百分点的波动。这些数字只能用来确认方法基本有效，不应作为正式的评测结果。实验脚本在 [`research/experiments/`](research/experiments/)。

## 目录结构

```
extension/            Chrome 扩展（Manifest V3）
  content.js          正文提取、按需处理、Custom Highlight 显示
  background.js       Native Messaging 连接、快捷键
  popup.html/.js      开关、按站点自动开启、设置、费用
host/
  skimlight_host.py   本地程序：分词、Jev 调用、SQLite 缓存、每日费用上限
skimlight/reader.py   分句分词与候选词生成（jieba / spaCy）、Jev 题目写法
scripts/              Windows 安装与卸载、本地程序自测、Playwright 端到端测试
tests/pages/          本地测试页面
research/             设计过程中的实验（运行时不需要）
```

## 开发

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium

.venv/bin/python scripts/host_selftest.py       # 用 Native Messaging 协议测试本地程序
.venv/bin/python scripts/e2e_test.py            # 在 Chromium 中加载扩展，测试 tests/pages/blog.html
.venv/bin/python scripts/e2e_test.py https://example.com/some-article   # 也可以测试真实网页
```

两个测试都会调用 Jev，需要先配置 API key。本地程序的日志在 `~/.cache/skimlight/host.log`。

修改扩展代码后，重新运行 `bash scripts/install_windows.sh`，再到 `chrome://extensions` 点击扩展的「重新加载」。

## 已知限制

- **正文定位**：用的是启发式规则，部分网站会把作者、单位信息也当作正文，或者漏掉部分正文。
- **中文分词**：jieba 可能切错专业术语，以及中英混写的名称。
- **首次开启的等待**：Chrome 启动后第一次开启时，本地程序需要加载模型，要等 2–4 秒。
- **暂不支持 PDF**：Chrome 自带的 PDF 查看器不允许扩展修改内容。

## 计划

- 不依赖 WSL 的 Windows 安装包（PyInstaller + Inno Setup）
- 设置页：填写 API key，选择服务商（OpenRouter 或 TypeSafe 官方）
- macOS 和 Linux 的本地程序注册
- 上架 Chrome 应用商店
- 基于 pdf.js 的 PDF 阅读页
- 模拟 Jev 的单元测试与 CI

## 许可证

代码采用 [MIT](LICENSE) 许可证。`research/data/` 和 `tests/pages/` 中的示例文本改编自维基百科，采用 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 许可证，详见 [research/data/README.md](research/data/README.md)。
