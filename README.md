# ScreenQA 答题助手

**屏幕区域 OCR + 大模型自动答题** — 框住屏幕上一块区域，内容一变就自动截图、本地 OCR 识别、发给大模型作答。
翻题快慢随你，框选之后全程不用再碰这个程序。

*Screen-region OCR + LLM quiz solver. Frame an area once, it auto-captures on change, runs local OCR, and answers with an LLM.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

![演示](docs/demo.png)

---

## 特性

- **自动触发** —— 不是定时轮询，而是盯画面变化。翻页停稳了才截图，停 2 秒还是 2 分钟都无所谓
- **本地 OCR** —— 用 RapidOCR（ONNX，自带中英文模型），只把识别出的**文字**发出去，截图不出本机
- **也支持多模态** —— 改一行配置就能把截图直接发给视觉模型，公式题、图表题更准
- **任意 OpenAI 兼容接口** —— DeepSeek、月之暗面、通义千问、本地 ollama，改 `base_url` 就行
- **固定输出格式** —— 只回【题目】【答案】【解析】三段，不说废话
- **截图行为完全可见** —— 三处同时告诉你有没有截图，见下文
- **全局热键** —— 框选完就锁定，暂停/重答/退出都不用切回窗口

## 工作原理

```
屏幕区域 ──> 变化检测 ──> 截图 ──> 本地 OCR ──> 文字 ──> 大模型 ──> 【题目】【答案】【解析】
            (画面指纹)           (RapidOCR)          (DeepSeek 等)
```

变化检测的具体做法：

1. 每 0.25 秒抓一次区域，压成 64×64 灰度矩阵当作画面指纹
2. 和上一帧比，明显变化的像素超过 1.2% 就判定"正在翻页"（滚动、动画、加载都算）
3. 画面不动了、且连续 2 帧都不动，才认为新题目稳定下来 —— 这时才截图
4. 稳定后的画面再和"上次已识别的画面"比一次，一样就不重复问

所以触发点是"内容变了并且停稳了"，不是时间。光标闪烁、鼠标划过会被阈值滤掉。
上一题没答完就翻页的话，旧请求自动作废。OCR 一个字都没读到时**不会**发请求，不浪费 token。

## 快速开始

```bash
git clone https://github.com/Thedan-1/ScreenQA-ocr-quiz-solver.git
cd ScreenQA-ocr-quiz-solver
pip install -r requirements.txt
python main.py
```

首次运行会自动从 `config.example.json` 生成 `config.json`，把 API key 填进去：

```json
"api": {
  "base_url": "https://api.deepseek.com",
  "api_key": "sk-你的密钥",
  "model": "deepseek-chat"
}
```

也可以留空 `api_key`，改用环境变量 `DEEPSEEK_API_KEY`。

> 程序**只**读 `api_key_env` 指定的那一个环境变量（默认 `DEEPSEEK_API_KEY`），
> 不会去捡 `OPENAI_API_KEY` 之类 —— 避免把一家的密钥发到另一家的服务器上。

启动后屏幕变暗，拖鼠标框出题目区域即可。

## 支持的模型

任何 OpenAI 兼容的 Chat Completions 接口：

| 服务 | `base_url` | `model` |
|------|-----------|---------|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 本地 ollama | `http://localhost:11434/v1` | `qwen2.5:14b` |

想跳过 OCR、直接把截图交给多模态模型，把 `backend` 改成 `"vision"`（需要 `pip install anthropic`）：

```json
"backend": "vision",
"vision": { "model": "claude-sonnet-5" }
```

## 怎么知道它有没有截图

这类工具最让人不安的是"它到底什么时候在截我的屏"。所以截图行为被设计成三处同时可见：

| 信号 | 表现 |
|------|------|
| **屏幕上的取景框** | 监视区域周围一圈暗色细线常驻；**真正截图的那一刻闪亮绿加粗**。框内完全透明，不挡鼠标点击 |
| **面板第二行** | `最近截图 20:41:04 · 660×268 · 本次共 3 张`；暂停时变成 `已暂停，不会再截图` |
| **面板底部原文** | 直接显示 OCR 读到的文字和置信度 —— 它到底截到了什么，你能亲眼看见 |

暂停（F8）后取景框变黄，程序不会再做任何截屏动作。

## 热键

框选完成后不需要再操作窗口，任何时候按都生效：

| 键 | 作用 |
|----|------|
| `F6` | 显示 / 隐藏识别原文 |
| `F7` | 显示 / 隐藏答案面板 |
| `F8` | 暂停 / 继续监听 |
| `F9` | 立刻重新识别当前画面 |
| `F10` | 重新框选区域 |
| `F11` | 显示 / 隐藏取景框 |
| `F12` | 退出 |

## 配置说明

| 字段 | 说明 |
|------|------|
| `backend` | `text` = 本地 OCR + 语言模型；`vision` = 截图直接给多模态模型 |
| `api.base_url` / `model` | OpenAI 兼容接口地址和模型名 |
| `api.api_key` | 密钥。留空则读 `api_key_env` 指定的环境变量 |
| `api.api_key_env` | 从哪个环境变量取密钥，默认 `DEEPSEEK_API_KEY` |
| `ocr.min_confidence` | 低于此置信度的识别结果丢弃，默认 0.5 |
| `ocr.upscale` | 小图放大倍数。小字识别不准时调到 3 |
| `region` | 框选区域。设为 `null` 会重新框选 |
| `diff_threshold` | 变化判定阈值。误触发多就调大，翻页识别不到就调小 |
| `stable_frames` | 连续几帧不动算稳定。页面动画长就调到 3~4 |
| `show_region_outline` / `show_ocr_text` | 取景框、识别原文的显示开关 |

`config.json` 已被 `.gitignore` 忽略（里面有密钥和你的屏幕坐标）。
编辑时请存成 **UTF-8 无 BOM**；存错了程序会在面板上明确提示，而不是默默用默认值。

## 常见问题

**识别不准？** 先按 F6 看识别原文。字太小就把 `ocr.upscale` 调到 3；区域框太大混进无关文字就缩小范围。

**一直不触发？** 区域里可能有视频、动图或跑动的进度条，会被当成"一直在翻页"。把框缩到只包住题干和选项。

**公式题、图表题读不出来？** OCR 处理不了这类内容，把 `backend` 改成 `vision`。

**第一次很慢？** OCR 引擎首次加载约 3 秒（启动时后台加载），之后每次识别约 0.5 秒。

**多显示器能用吗？** 可以，包括副屏在主屏左侧（负坐标）的情况。

## 项目结构

```
screenqa/
├── app.py        主程序：配置、事件循环、热键
├── capture.py    截屏 + 画面变化检测状态机
├── ocr.py        本地 OCR，按行还原文字
├── solver.py     两种后端、提示词、流式输出、旧任务作废
└── ui.py         框选遮罩 + 取景框 + 答案面板
```

## 环境

Windows 10/11，Python 3.8+。DPI 感知、全局热键、取景框透明都走 Windows 路径，其他系统未验证。

---

## English

**ScreenQA** watches a screen region you frame once. When the content changes and settles,
it captures the region, runs **local OCR** (RapidOCR — Chinese + English, ONNX, no PaddlePaddle/Torch),
and sends the recognized **text** to an LLM. Only text leaves your machine; the screenshot does not.

- Change-triggered, not polled — flip pages at any pace
- Works with any OpenAI-compatible endpoint (DeepSeek, Moonshot, Qwen, local ollama)
- Optional `vision` backend sends the screenshot straight to a multimodal model
- Fixed three-part output: question / answer / short explanation
- Capture activity is visible in three places at once (see the table above) — the region outline
  flashes green the moment a screenshot is taken, and the OCR text is shown verbatim
- Global hotkeys, no window interaction after framing

```bash
pip install -r requirements.txt
python main.py     # or: python -m screenqa
```

Put your key in `config.json` (`api.api_key`) or in the `DEEPSEEK_API_KEY` environment variable.
The program reads **only** the env var named by `api_key_env` — it never falls back to
`OPENAI_API_KEY`, so a key for one vendor is never sent to another.

## 使用须知 / Disclaimer

本项目是屏幕 OCR 与大模型调用的技术实践，适合用于自动化流程测试、资料速查、学习辅助。
请勿用于正式考试、认证测评等明确禁止外部辅助的场合 —— 是否合规由使用者自行判断并承担责任。

*This is a technical demo of screen OCR + LLM integration, intended for automation testing,
quick reference, and study support. Do not use it in exams, certifications, or any setting
where outside assistance is prohibited. Compliance is the user's responsibility.*

## 关键词

自动答题 · 答题助手 · 屏幕识别 · 截图识别 · OCR 答题 · 题目识别 · AI 答题 · 大模型答题 · DeepSeek
screen ocr · quiz solver · auto answer · screenshot ocr · llm question answering · rapidocr · python tkinter

## License

[MIT](LICENSE)
