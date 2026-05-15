<div align="center">
<img src="assets/images/bar.jpg" width="880"/>

<a href="https://trendshift.io/repositories/25944" target="_blank"><img src="https://trendshift.io/api/badge/repositories/25944" alt="lsdefine%2FGenericAgent | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>

</div>

<p align="center">
  <a href="#english">English</a> | <a href="#chinese">中文</a> | 📄 Technical Report:&nbsp;<a href="https://arxiv.org/abs/2604.17091"><img src="https://img.shields.io/badge/arXiv-2604.17091-b31b1b?logo=arxiv&logoColor=white" alt="arXiv" height="18"/></a>&nbsp;<a href="assets/GenericAgent_Technical_Report.pdf"><img src="https://img.shields.io/badge/-PDF-EA4335?logo=adobeacrobatreader&logoColor=white" alt="Technical Report PDF" height="18"/></a>&nbsp;<a href="https://github.com/JinyiHan99/GA-Technical-Report"><img src="https://img.shields.io/badge/-Code%20%26%20Data-181717?logo=github&logoColor=white" alt="Experiments & Reproduction Repo" height="18"/></a> | 📘 <a href="https://datawhalechina.github.io/hello-generic-agent/">教程</a> | <a href="https://fudankw.cn/sophub">Sophub</a>
</p>


---
<a name="english"></a>
## 🌟 Overview

**GenericAgent** is a compact autonomous agent prototype built around a small Python runtime, a static function-tool schema, and a short agent loop. It can drive a local computer through browser, terminal, filesystem, keyboard/mouse input, screen vision, and mobile devices (ADB), but the current code does **not** justify treating it as a reliable self-evolving Skill/MCP platform.

<span style="background-color: #e6f2ff; color: #0f172a; padding: 0.12em 0.25em; border-radius: 4px;"><strong>This fork keeps the useful minimal-runtime idea while explicitly rejecting the stronger upstream framing that a latest large model plus roughly 3K lines of code is enough to safely "grow" stable external capabilities. In particular, GPT-5.5-class model strength should not be used to paper over missing tool governance, missing MCP/Skill integration, or unsafe ad-hoc code execution.</strong></span>

The current implementation is closer to: **static core tools + memory/SOP notes + model-written scripts on demand**.

## 📋 Core Features
- **Memory / SOP accumulation**: The agent can write working checkpoints, long-term notes, and SOP-like files, but this is not the same as dependable automatic Skill creation.
- **Minimal Architecture**: ~3K lines of core code. Agent Loop is ~100 lines. No complex dependencies, zero deployment overhead.
- **Strong Execution**: Injects into a real browser (preserving login sessions) and exposes local execution/file/browser tools. This power needs review and sandboxing when used on real machines.
- **No native MCP / Agent Skill standard**: The current repository has no first-class MCP server/client loader and no mature Skill package loader. Existing "skills" are mostly memory/SOP files plus a local search helper.
- **High Compatibility**: Supports Claude / Gemini / Kimi / MiniMax and other major models. Cross-platform.
- **Token Efficient**: <30K context window — a fraction of the 200K–1M other agents consume. Layered memory ensures the right knowledge is always in scope. Less noise, fewer hallucinations, higher success rate — at a fraction of the cost.


## 🧬 Self-Evolution Caveats

Code review of the current repository shows several important limits:

- The tool list is not actually a clear "9 atomic tools" set. `assets/tools_schema*.json` currently exposes 10 function tools: `code_run`, `file_read`, `file_patch`, `file_write`, `web_scan`, `web_execute_js`, `update_working_checkpoint`, `ask_user`, `codex_lesson_update`, and `start_long_term_update`. Several of these are memory or distillation helpers, not atomic environment tools.
- There is no native MCP integration path. The ACP bridge reports MCP HTTP/SSE capability as false, and tool dispatch only calls local `do_<tool>` methods.
- There is no mature external Agent Skill runtime. Memory files, SOP markdown, and `memory/skill_search` can help recall workflows, but they do not provide the packaging, manifesting, permissioning, invocation, and lifecycle model expected from modern Skill systems.
- New capabilities are often created by having the model write or run code through `code_run`. That is useful for exploration, but it is unstable and dangerous as a primary evolution mechanism because generated code executes locally and is not automatically sandboxed, reviewed, versioned, or permission-scoped.
- "Evolution" is not automatically triggered after every successful task. Long-term memory updates require the model to call `start_long_term_update`; autonomous tasks are triggered only by specific frontends/scripts such as Qt idle mode or `--reflect`, not by a general verified-success pipeline.

The safer interpretation is therefore: GenericAgent can accumulate notes and scripts, but the current implementation should not be described as a dependable self-evolving Skill tree.


##### 🎯 Demo Showcase

|                                           🧋 Food Delivery Order                                            |                                      📈 Quantitative Stock Screening                                       |
| :--------------------------------------------------------------------------------------------------------: | :-------------------------------------------------------------------------------------------------------: |
|                     <img src="assets/demo/order_tea.gif" width="100%" alt="Order Tea">                     |                <img src="assets/demo/selectstock.gif" width="100%" alt="Stock Selection">                 |
| *"Order me a milk tea"* — Navigates the delivery app, selects items, and completes checkout automatically. | *"Find GEM stocks with EXPMA golden cross, turnover > 5%"* — Screens stocks with quantitative conditions. |
|                                        🌐 Autonomous Web Exploration                                        |                                            💰 Expense Tracking                                             | 💬 Batch Messaging                                                        |
|             <img src="assets/demo/autonomous_explore.png" width="100%" alt="Web Exploration">              |               <img src="assets/demo/alipay_expense.png" width="100%" alt="Alipay Expense">                | <img src="assets/demo/wechat_batch.png" width="100%" alt="WeChat Batch"> |
|                       Autonomously browses and periodically summarizes web content.                        |                 *"Find expenses over ¥2K in the last 3 months"* — Drives Alipay via ADB.                  | Sends bulk WeChat messages, fully driving the WeChat client.             |

## 📅 Latest News

- **2026-04-21:** 📄 [Technical Report released on arXiv](https://arxiv.org/abs/2604.17091) — *GenericAgent: A Token-Efficient Self-Evolving LLM Agent via Contextual Information Density Maximization*
- **2026-04-11:** Introduced **L4 session archive memory** and scheduler cron integration
- **2026-03-23:** Support personal WeChat as a bot frontend
- **2026-03-10:** [Released million-scale Skill Library](https://mp.weixin.qq.com/s/q2gQ7YvWoiAcwxzaiwpuiQ?scene=1&click_id=7)
- **2026-03-08:** [Released "Dintal Claw" — a GenericAgent-powered government affairs bot](https://mp.weixin.qq.com/s/eiEhwo-j6S-WpLxgBnNxBg)
- **2026-03-01:** [GenericAgent featured by Jiqizhixin (机器之心)](https://mp.weixin.qq.com/s/uVWpTTF5I1yzAENV_qm7yg)
- **2026-01-16:** GenericAgent V1.0 public release

---

## 🚀 Quick Start

#### Method 1: Standard Installation

```bash
# 1. Clone the repo
git clone https://github.com/lsdefine/GenericAgent.git
cd GenericAgent

# 2. Install dependencies
pip install requests streamlit pywebview   # Desktop GUI (launch.pyw)
pip install requests textual               # Terminal UI (tuiapp.py)

# 3. Configure API Key
cp mykey_template.py mykey.py
# Edit mykey.py and fill in your LLM API Key

# 4. Launch
python launch.pyw
```

#### Method 2: uv (for experienced Python users)

If you prefer a modern Python workflow, GenericAgent also provides a minimal `pyproject.toml`:

```bash
git clone https://github.com/lsdefine/GenericAgent.git
cd GenericAgent
uv venv
uv pip install -e ".[ui]"        # Core + GUI dependencies
cp mykey_template.py mykey.py
python launch.pyw
```

> GenericAgent is meant to grow its environment through the Agent itself, not by pre-installing every possible package.

Full guide: [GETTING_STARTED.md](GETTING_STARTED.md)

---

## 🖥️ Desktop Frontends

### Terminal UI

A lightweight, keyboard-driven interface built on [Textual](https://github.com/Textualize/textual). Supports multiple concurrent sessions, real-time streaming, and runs anywhere a terminal does — no browser needed.

```bash
python frontends/tuiapp.py
```

### Other Desktop Frontends

```bash
python frontends/qtapp.py                # Qt-based desktop app
streamlit run frontends/stapp2.py        # Alternative Streamlit UI
```

### Codeg

<table><tr>
<td width="70%">

[Codeg](https://github.com/yiqi-017/codeg) (`feat/genericagent-integration` branch) is a desktop/web UI that connects GenericAgent alongside other agents (Claude Code, Gemini, Codex, etc.) in a unified interface with a polished, modern UI.

> This integration is usable now. Some features are still being refined — feedback welcome.

Place your GenericAgent directory alongside the codeg project. Codeg will auto-detect `frontends/genericagent_acp_bridge.py` and launch GenericAgent as a local ACP agent.

</td>
<td width="30%">
<img src="assets/demo/codeg-demo.gif" width="90%" alt="Codeg Demo">
</td>
</tr></table>

---

## 💬 Bot Interface (IM)

### Telegram Bot

```python
# mykey.py
tg_bot_token = 'YOUR_BOT_TOKEN'
tg_allowed_users = [YOUR_USER_ID]
```

```bash
python frontends/tgapp.py
```

### Common Chat Commands

The default Streamlit desktop UI started by `python launch.pyw`, plus the QQ / Telegram / Feishu / WeCom / DingTalk frontends, support these chat commands:

- `/new` - start a fresh conversation and clear the current context
- `/continue` - list recoverable conversation snapshots
- `/continue N` - restore the `N`th recoverable conversation


## 📊 Comparison with Similar Tools

| Feature             |                     GenericAgent                     |          OpenClaw           |        Claude Code         |
| ------------------- | :--------------------------------------------------: | :-------------------------: | :------------------------: |
| **Codebase**        |                      ~3K lines                       |       ~530,000 lines        |    Open-sourced (large)    |
| **Deployment**      |               `pip install` + API Key                | Multi-service orchestration |     CLI + subscription     |
| **Browser Control** |           Real browser (session preserved)           | Sandbox / headless browser  |       Via MCP plugin       |
| **OS Control**      |                Mouse/kbd, vision, ADB                |   Multi-agent delegation    |      File + terminal       |
| **Self-Evolution**  | Memory/SOP accumulation; no mature MCP/Skill runtime |      Plugin ecosystem       | Stateless between sessions |
| **Out of the Box**  |          A few core files + starter skills           |     Hundreds of modules     |      Rich CLI toolset      |


## 📈 Evaluation — Five Dimensions

> 📂 Full evaluation datasets and results: <https://github.com/JinyiHan99/GA-Technical-Report/tree/main>

| Dimension                                 | Question                                                                                      | Benchmarks used                                                       |
| ----------------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| **1. Task Completion & Token Efficiency** | Can GA complete hard tasks more cheaply than leading agents?                                  | SOP-Bench, Lifelong AgentBench, RealFin-Benchmark                     |
| **2. Tool-Use Efficiency**                | Can a minimal atomic toolset solve what specialized toolsets solve, with less overhead?       | Tool Efficiency Benchmark (11 simple + 5 long-horizon tasks)          |
| **3. Memory System Effectiveness**        | Does condensed hierarchical memory beat full/redundant memory and embedding-based retrievers? | SOP-Bench (dangerous goods), LoCoMo, 20-skill stress test             |
| **4. Self-Evolution Capability**          | Can the agent distill experience into reusable SOPs and code, without intervention?           | 9-round LangChain longitudinal study, 8-task cross-task web benchmark |
| **5. Web Browsing Capability**            | Does density-driven design survive the open web?                                              | WebCanvas, BrowseComp-ZH, Custom Tasks (22)                           |

Baselines across these dimensions include **Claude Code**, **OpenAI CodeX**, and **OpenClaw**, evaluated under *Claude Sonnet 4.6*, *Claude Opus 4.6*, *GPT-5.4*, and *MiniMax M2.7* backbones.

<table>
  <tr>
    <td align="center" width="50%">
      <img src="assets/images/result_radar.png" width="100%" alt="Tool-use efficiency radar"/><br/>
      <sub><b>Tool-use efficiency radar.</b> GA dominates token, request, and tool-call axes while preserving quality across four task dimensions.</sub>
    </td>
    <td align="center" width="50%">
      <img src="assets/images/result_convergence.png" width="100%" alt="Cross-task self-evolution convergence"/><br/>
      <sub><b>Cross-task self-evolution.</b> Second- and third-run GA executions converge to a stable low-cost regime across eight web tasks, while OpenClaw shows no such convergence.</sub>
    </td>
  </tr>
</table>


## 🧠 How It Works

GenericAgent accomplishes complex tasks through **Layered Memory × Minimal Toolset × Autonomous Execution Loop**, continuously accumulating experience during execution.

1️⃣ **Layered Memory System**
> _Memory crystallizes throughout task execution, letting the agent build stable, efficient working patterns over time._

- **L0 — Meta Rules**: Core behavioral rules and system constraints of the agent
- **L1 — Insight Index**: Minimal memory index for fast routing and recall
- **L2 — Global Facts**: Stable knowledge accumulated over long-term operation
- **L3 — Task Skills / SOPs**: Reusable workflows for completing specific task types
- **L4 — Session Archive**: Archived task records distilled from finished sessions for long-horizon recall

2️⃣ **Autonomous Execution Loop**

> _Perceive environment state → Task reasoning → Execute tools → Write experience to memory → Loop_

The entire core loop is just **~100 lines of code** (`agent_loop.py`).

3️⃣ **Minimal Toolset**
> _GenericAgent currently uses a static JSON tool schema. The README used to call this "9 atomic tools", but the repository now exposes 10 function tools and mixes environment tools with memory/distillation helpers._

| Tool                        | Function                               |
| --------------------------- | -------------------------------------- |
| `code_run`                  | Execute arbitrary code                 |
| `file_read`                 | Read files                             |
| `file_write`                | Write files                            |
| `file_patch`                | Patch / modify files                   |
| `web_scan`                  | Perceive web content                   |
| `web_execute_js`            | Control browser behavior               |
| `ask_user`                  | Human-in-the-loop confirmation         |
| `update_working_checkpoint` | Store short-term working memory        |
| `start_long_term_update`    | Start long-term memory distillation    |
| `codex_lesson_update`       | Record Codex-session lesson candidates |

Only the first group are basic environment controls. The remaining tools are memory/distillation controls, so they should not be counted as the same kind of atomic external capability.

4️⃣ **Capability Extension Mechanism**
> _Capable of asking the model to write and run scripts, but not a governed external capability system._

Via `code_run`, GenericAgent can dynamically install Python packages, write new scripts, call external APIs, or control hardware at runtime. This should be treated as ad-hoc local code execution, not as a safe replacement for MCP or a mature Agent Skill framework.

<div align="center">
  <img src="assets/images/workflow.jpg" alt="GenericAgent Workflow" width="400"/>
  <br><em>GenericAgent Workflow Diagram</em>
</div>


## ⭐ Support

If this project helped you, please consider leaving a **Star!** 🙏

## 📄 License

MIT License — see [LICENSE](LICENSE)

*Disclaimer: This project does not build or operate any commercial website. Apart from DintalClaw, no institution, organization, or individual is currently officially authorized to conduct commercial activities under the GenericAgent name.*


---
<a name="chinese"></a>
## 🌟 项目简介

**GenericAgent** 是一个紧凑的自主 Agent 原型：它围绕少量 Python 运行时代码、静态函数工具 schema 和一个短 Agent Loop，给 LLM 提供浏览器、终端、文件系统、键鼠输入、屏幕视觉及移动设备（ADB）等本地控制能力。但从当前代码看，它还不能被严肃地描述为可靠的自我进化 Skill/MCP 平台。

<span style="background-color: #e6f2ff; color: #0f172a; padding: 0.12em 0.25em; border-radius: 4px;"><strong>本分支保留“极简运行时”的实验价值，同时明确反对上游把“约 3K 行代码 + 最新强模型”包装成可靠自我进化系统的叙事。本人不认同目前站在 GPT-5.5 级别模型能力上、靠所谓 3000 行代码杂耍和现场 vibe coding 生成脚本来承担成熟外部能力接入的做法。</strong></span>

当前实现更准确的描述是：**静态核心工具 + 记忆/SOP 笔记 + 按需由模型编写并执行脚本**。

## 📋 核心特性
- **记忆 / SOP 沉淀**: Agent 可以写入工作便签、长期记忆和 SOP 类文件，但这不等于稳定、自动、可治理的 Skill 生成。
- **极简架构**: ~3K 行核心代码，Agent Loop 约百行，无复杂依赖，部署零负担
- **强执行力**: 注入真实浏览器（保留登录态），并暴露本地执行、文件、浏览器等工具。这个能力在真实机器上使用时需要审查、沙箱和权限边界。
- **不支持原生 MCP / Agent Skill 标准**: 当前仓库没有一等 MCP server/client 加载路径，也没有成熟 Skill 包加载器；现有“skills”主要是记忆/SOP 文件和本地检索辅助。
- **高兼容性**: 支持 Claude / Gemini / Kimi / MiniMax 等主流模型，跨平台运行
- **极致省 Token**: 上下文窗口不到 30K，是其他 Agent（200K–1M）的零头。分层记忆让关键信息始终在场——噪声更少，幻觉更低，成功率反而更高，而成本低一个数量级。

## 🧬 自我进化限制

按当前代码核对，README 里原来的自我进化表述需要降级为限制说明：

- 工具数量并不是清晰的“9 个原子工具”。`assets/tools_schema*.json` 当前实际暴露 10 个函数工具：`code_run`、`file_read`、`file_patch`、`file_write`、`web_scan`、`web_execute_js`、`update_working_checkpoint`、`ask_user`、`codex_lesson_update`、`start_long_term_update`。其中不少是记忆或蒸馏辅助，不是同一层级的环境原子工具。
- 当前没有原生 MCP 接入路径。ACP bridge 明确声明 MCP HTTP/SSE 能力为 false，工具分发也只是调用本地 `do_<tool>` 方法。
- 当前没有成熟的外部 Agent Skill 运行时。记忆文件、SOP markdown 和 `memory/skill_search` 可以帮助召回流程，但不具备现代 Skill 系统应有的包结构、manifest、权限、调用和生命周期管理。
- 新能力通常依赖模型通过 `code_run` 当场写脚本、装依赖、调外部 API 或控制硬件。这对探索有用，但作为“进化机制”并不稳定，也有安全风险，因为生成代码会在本机执行，缺少自动沙箱、审查、版本化和权限隔离。
- “进化”不是每个任务成功后自动触发。长期记忆更新依赖模型主动调用 `start_long_term_update`；自主行动只在 Qt idle 模式或 `--reflect` 等特定入口中触发，不是一个通用的、验证成功后自动沉淀 Skill 的流水线。

因此，更安全准确的说法是：GenericAgent 可以积累笔记、SOP 和脚本，但当前实现不应被宣传为可靠的自我进化技能树。

<!-- | *"帮我读取微信消息"* | 安装依赖 → 逆向数据库 → 写读取脚本 → 保存 Skill | **一句话调用** | -->

#### 🎯 实例展示

|                                    🧋 外卖下单                                     |                                  📈 量化选股                                  |
| :-------------------------------------------------------------------------------: | :--------------------------------------------------------------------------: |
|        <img src="assets/demo/order_tea.gif" width="100%" alt="Order Tea">         |  <img src="assets/demo/selectstock.gif" width="100%" alt="Stock Selection">  |
|            *"Order me a milk tea"* — 自动导航外卖 App，选品并完成结账             |  *"Find GEM stocks with EXPMA golden cross, turnover > 5%"* — 量化条件筛股   |
|                                  🌐 自主网页探索                                   |                                  💰 支出追踪                                  | 💬 批量消息                                                               |
| <img src="assets/demo/autonomous_explore.png" width="100%" alt="Web Exploration"> | <img src="assets/demo/alipay_expense.png" width="100%" alt="Alipay Expense"> | <img src="assets/demo/wechat_batch.png" width="100%" alt="WeChat Batch"> |
|                            自主浏览并定时汇总网页信息                             |             *"查找近 3 个月超 ¥2K 的支出"* — 通过 ADB 驱动支付宝             | 批量发送微信消息，完整驱动微信客户端                                     |



## 📅 最新动态

- **2026-04-21:** 📄 [技术报告已发布至 arXiv](https://arxiv.org/abs/2604.17091) — *GenericAgent: A Token-Efficient Self-Evolving LLM Agent via Contextual Information Density Maximization*
- **2026-04-11:** 引入 **L4 会话归档记忆**，并接入 scheduler cron 调度
- **2026-03-23:** 支持个人微信接入作为 Bot 前端
- **2026-03-10:** [发布百万级 Skill 库](https://mp.weixin.qq.com/s/q2gQ7YvWoiAcwxzaiwpuiQ?scene=1&click_id=7)
- **2026-03-08:** [发布以 GenericAgent 为核心的"政务龙虾" Dintal Claw](https://mp.weixin.qq.com/s/eiEhwo-j6S-WpLxgBnNxBg)
- **2026-03-01:** [GenericAgent 被机器之心报道](https://mp.weixin.qq.com/s/uVWpTTF5I1yzAENV_qm7yg)
- **2026-01-16:** GenericAgent V1.0 公开版本发布

---

## 🚀 快速开始

#### 方法一：标准安装

```bash
# 1. 克隆仓库
git clone https://github.com/lsdefine/GenericAgent.git
cd GenericAgent

# 2. 安装依赖
pip install requests streamlit pywebview   # 桌面 GUI (launch.pyw)
pip install requests textual               # 终端 UI (tuiapp.py)

# 3. 配置 API Key
cp mykey_template.py mykey.py
# 编辑 mykey.py，填入你的 LLM API Key
# 或使用交互式向导：python assets/configure_mykey.py

# 4. 启动
python launch.pyw
```

#### 方法二：uv 快速安装（熟悉 Python 的用户）

如果你习惯现代 Python 工作流，GenericAgent 也提供了一个最小化的 `pyproject.toml`：

```bash
git clone https://github.com/lsdefine/GenericAgent.git
cd GenericAgent
uv pip install -e ".[ui]"        # 核心 + GUI 依赖
cp mykey_template.py mykey.py
python launch.pyw
```

> GenericAgent 更推荐由 Agent 在使用中自举环境，而不是预先手动装完整依赖。

完整引导流程见 [GETTING_STARTED.md](GETTING_STARTED.md)。

📖 新手使用指南（图文版）：[飞书文档](https://my.feishu.cn/wiki/CGrDw0T76iNFuskmwxdcWrpinPb)

📘 完整入门教程（Datawhale 出品）：[Hello GenericAgent](https://datawhalechina.github.io/hello-generic-agent/) · [GitHub](https://github.com/datawhalechina/hello-generic-agent)

---

## 🖥️ 桌面前端

### 终端 UI

基于 [Textual](https://github.com/Textualize/textual) 的轻量键盘驱动界面。支持多会话并发、实时流式输出，有终端就能跑，无需浏览器。

```bash
python frontends/tuiapp.py
```

### 其他桌面前端

```bash
python frontends/qtapp.py                # 基于 Qt 的桌面应用
streamlit run frontends/stapp2.py        # 另一种 Streamlit 风格 UI
```

### Codeg前端

<table><tr>
<td width="70%">

[Codeg](https://github.com/yiqi-017/codeg)（`feat/genericagent-integration` 分支）是一个桌面/Web UI，可以将 GenericAgent 与其他代理（Claude Code、Gemini、Codex 等）在统一界面中并行使用，UI 更加精美。

> 此集成已可使用，部分功能仍在完善中，欢迎体验反馈。

将 GenericAgent 目录放在 codeg 项目同级目录下，Codeg 会自动检测 `frontends/genericagent_acp_bridge.py` 并将 GenericAgent 作为本地 ACP 代理启动。

</td>
<td width="30%">
<img src="assets/demo/codeg-demo.gif" width="90%" alt="Codeg Demo">
</td>
</tr></table>

---

## 💬 Bot 接口（IM）

### 微信 Bot（个人微信）

无需额外配置，扫码登录即可：

```bash
pip install pycryptodome qrcode requests
python frontends/wechatapp.py
```

> 首次启动会弹出二维码，用微信扫码完成绑定。之后通过微信消息与 Agent 交互。

### QQ Bot

使用 `qq-botpy` WebSocket 长连接，**无需公网 webhook**：

```bash
pip install qq-botpy
```

在 `mykey.py` 中补充：

```python
qq_app_id = "YOUR_APP_ID"
qq_app_secret = "YOUR_APP_SECRET"
qq_allowed_users = ["YOUR_USER_OPENID"]  # 或 ['*'] 公开访问
```

```bash
python frontends/qqapp.py
```

> 在 [QQ 开放平台](https://q.qq.com) 创建机器人获取 AppID / AppSecret。首次消息后，用户 openid 记录于 `temp/qqapp.log`。

### 飞书（Lark）

```bash
pip install lark-oapi
python frontends/fsapp.py
```

```python
fs_app_id = "cli_xxx"
fs_app_secret = "xxx"
fs_allowed_users = ["ou_xxx"]  # 或 ['*']
```

**入站支持**：文本、富文本 post、图片、文件、音频、media、交互卡片 / 分享卡片  
**出站支持**：流式进度卡片、图片回传、文件 / media 回传  
**视觉模型**：图片首轮以真正的多模态输入发送给兼容 OpenAI Vision 的后端

详细配置见 [assets/SETUP_FEISHU.md](assets/SETUP_FEISHU.md)


### 企业微信（WeCom）

```bash
pip install wecom_aibot_sdk
python frontends/wecomapp.py
```

```python
wecom_bot_id = "your_bot_id"
wecom_secret = "your_bot_secret"
wecom_allowed_users = ["your_user_id"]
wecom_welcome_message = "你好，我在线上。"
```

### 钉钉（DingTalk）

```bash
pip install dingtalk-stream
python frontends/dingtalkapp.py
```

```python
dingtalk_client_id = "your_app_key"
dingtalk_client_secret = "your_app_secret"
dingtalk_allowed_users = ["your_staff_id"]  # 或 ['*']
```

### 通用聊天命令

默认通过 `python launch.pyw` 启动的 Streamlit 桌面 UI，以及 QQ / Telegram / 飞书 / 企业微信 / 钉钉前端，都支持以下命令：

- `/new` - 开启新对话并清空当前上下文
- `/continue` - 列出可恢复会话快照
- `/continue N` - 恢复第 `N` 个可恢复会话


## 📊 与同类产品对比

| 特性           |              GenericAgent              |     OpenClaw      |   Claude Code    |
| -------------- | :------------------------------------: | :---------------: | :--------------: |
| **代码量**     |                 ~3K 行                 |    ~530,000 行    | 已开源（体量大） |
| **部署方式**   |        `pip install` + API Key         |    多服务编排     |    CLI + 订阅    |
| **浏览器控制** |      注入真实浏览器（保留登录态）      | 沙箱 / 无头浏览器 |  通过 MCP 插件   |
| **OS 控制**    |            键鼠、视觉、ADB             |   多 Agent 委派   |   文件 + 终端    |
| **自我进化**   | 记忆/SOP 沉淀；无成熟 MCP/Skill 运行时 |     插件生态      |   会话间无状态   |
| **出厂配置**   |     几个核心文件 + 少量初始 Skills     |     数百模块      | 丰富 CLI 工具集  |


## 📈 评测 — 五大维度

> 📂 完整的评测数据集以及评测结果见：<https://github.com/JinyiHan99/GA-Technical-Report/tree/main>

| 维度                           | 核心问题                                                | 使用的基准                                        |
| ------------------------------ | ------------------------------------------------------- | ------------------------------------------------- |
| **1. 任务完成度与 Token 效率** | GA 能否以更低成本完成高难度任务？                       | SOP-Bench、Lifelong AgentBench、RealFin-Benchmark |
| **2. 工具使用效率**            | 最小原子工具集能否以更低开销替代专用工具集？            | Tool Efficiency Benchmark                         |
| **3. 记忆系统有效性**          | 精简分层记忆能否超越冗余记忆和基于 Embedding 的检索器？ | SOP-Bench、LoCoMo、20-skill 压力测试              |
| **4. 自我进化能力**            | Agent 能否在无人干预下将经验提炼为可复用的 SOP 与代码？ | 9 轮 LangChain 纵向研究、8 任务跨任务 Web 基准    |
| **5. 网页浏览能力**            | 信息密度驱动设计能否适应开放网页？                      | WebCanvas、BrowseComp-ZH、自定义任务              |

以上维度的基线包括 **Claude Code**、**OpenAI CodeX** 和 **OpenClaw**，分别在 *Claude Sonnet 4.6*、*Claude Opus 4.6*、*GPT-5.4* 和 *MiniMax M2.7* 底座上进行评测。

<table>
  <tr>
    <td align="center" width="50%">
      <img src="assets/images/result_radar.png" width="100%" alt="工具使用效率雷达图"/><br/>
      <sub><b>工具使用效率雷达图。</b>GA 在 Token、请求数和工具调用轴上全面领先，同时在四个任务维度上保持质量。</sub>
    </td>
    <td align="center" width="50%">
      <img src="assets/images/result_convergence.png" width="100%" alt="跨任务自我进化收敛曲线"/><br/>
      <sub><b>跨任务自我进化。</b>GA 的第二轮和第三轮执行在 8 个 Web 任务上收敛至稳定的低成本区间。</sub>
    </td>
  </tr>
</table>


## 🧠 工作机制

GenericAgent 通过**分层记忆 × 最小工具集 × 自主执行循环**完成复杂任务，并在执行过程中持续积累经验。

1️⃣ **分层记忆系统**
> 记忆在任务执行过程中持续沉淀，使 Agent 逐步形成稳定且高效的工作方式


- **L0 — 元规则（Meta Rules）**：Agent 的基础行为规则和系统约束
- **L1 — 记忆索引（Insight Index）**：极简索引层，用于快速路由与召回
- **L2 — 全局事实（Global Facts）**：在长期运行过程中积累的稳定知识
- **L3 — 任务 Skills / SOPs**：完成特定任务类型的可复用流程
- **L4 — 会话归档（Session Archive）**：从已完成任务中提炼出的归档记录，用于长程召回

2️⃣ **自主执行循环**

> 感知环境状态  →  任务推理  →  调用工具执行  →  经验写入记忆  →  循环

整个核心循环仅 **约百行代码**（`agent_loop.py`）。

3️⃣ **最小工具集**
> GenericAgent 当前使用静态 JSON 工具 schema。README 过去称其为“9 个原子工具”，但仓库现状实际暴露 10 个函数工具，并且混合了环境控制工具、记忆工具和蒸馏工具。

| 工具                        | 功能                    |
| --------------------------- | ----------------------- |
| `code_run`                  | 执行任意代码            |
| `file_read`                 | 读取文件                |
| `file_write`                | 写入文件                |
| `file_patch`                | 修改文件                |
| `web_scan`                  | 感知网页内容            |
| `web_execute_js`            | 控制浏览器行为          |
| `ask_user`                  | 人机协作确认            |
| `update_working_checkpoint` | 写入短期工作便签        |
| `start_long_term_update`    | 启动长期记忆提炼        |
| `codex_lesson_update`       | 记录 Codex 会话经验候选 |

只有前一组更接近基础环境控制能力；后几项属于记忆/蒸馏控制，不应和外部世界原子工具混为一谈。

4️⃣ **能力扩展机制**
> 可以让模型编写并运行脚本，但这不是有治理的外部能力系统。
>
通过 `code_run`，GenericAgent 可在运行时动态安装 Python 包、编写新脚本、调用外部 API 或控制硬件。这里应被视为临时本地代码执行，而不是 MCP 或成熟 Agent Skill 框架的安全替代品。

<div align="center">
  <img src="assets/images/workflow.jpg" alt="GenericAgent 工作流程" width="400"/>
  <br><em>GenericAgent 工作流程图</em>
</div>



## 📄 许可
MIT License — 详见 [LICENSE](LICENSE)


## 📈 Star History

<a href="https://star-history.com/#lsdefine/GenericAgent&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=lsdefine/GenericAgent&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=lsdefine/GenericAgent&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=lsdefine/GenericAgent&type=Date" />
 </picture>
</a>
