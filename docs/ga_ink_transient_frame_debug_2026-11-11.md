# Ink 瞬时帧调试方法（2026-11-11）

> 本文记录一次真实的排查过程：用户报「agent team 浮层里出现了 workflow 子代理，一闪而过」。
> 排查没有完全收敛（界面来源未定位），但过程中确立了一套可复用的方法，以及几条代价高昂的教训。
> 后半部分的「失败路径」和「教训」与前半部分的「有效方法」同等重要 —— 本文的价值主要在后半部分。

---

## 1. 问题特征

用户描述的现象，决定了必须用什么方法：

- 面板**一闪而过**，肉眼读不完、截图来不及；
- 它能被一段**确定的按键序列**触发（乱码 + Enter + Esc）；
- 它显示的内容（任务标题）与真实数据吻合，但渲染代码在仓库里搜不到。

这类问题的本质是：**bug 存在于跨多次 render 的状态时序里**，而不是某一个纯函数的输出里。所以纯函数单测看不见它，人眼看不清它。

---

## 2. 有效方法

### 2.1 先确认「谁在跑」，再读代码

**这是本次排查中最有价值的一步，也是最容易漏掉的一步。**

在本仓库，`ga` 命令走的是 `C:\anaconda3\Scripts\ga.exe` → **site-packages 里的 wheel**，而不是当前目录的源码。所以「在源码目录执行 ga」不等于「跑的是源码」。

用进程信息确认实际加载路径（比读代码可靠）：

```bash
python -c "
import psutil
for p in psutil.process_iter(['pid','cmdline','cwd','create_time']):
    try:
        cl = p.info['cmdline'] or []
        j = ' '.join(cl)
        if 'psutil' in j: continue
        if any(k in j for k in ('main.tsx','ink','ga_cli','genericagent.core.app','bridge.py')):
            print(p.info['pid'], p.info['cwd'])
            print('  ', j[:200])
    except Exception: pass
"
```

实测输出（本次排查）：

```
PID 29984  cwd: C:\anaconda3\Lib\site-packages\frontends\ink\ui
           node ...tsx/dist/cli.mjs ...\frontends/ink/ui/src/main.tsx
PID 37136  cwd: C:\anaconda3\Lib\site-packages\frontends\ink\ui
           python ...\frontends\ink\bridge.py
```

结论：界面是**安装版代码**画的。任何在源码树里的搜索都必须换到 `site-packages` 里再做一遍。

顺带能拿到进程环境，判断它读的是哪份配置：

```python
env = psutil.Process(pid).environ()
for k in ('GA_PROJECT_ROOT','GA_DATA_DIR','GA_LLM_CONFIG','GA_TEAM_NAME'):
    print(k, env.get(k))
```

### 2.2 程序驱动 UI + 抓原始字节（`scripts/probe_ink_frames.py`）

人眼看不清的帧，程序能完整留档。脚本做三件事：

1. 用**和 `ga` 完全相同的入口**启动（`tsx src/main.tsx`，cwd 设为 UI 目录）；
2. 按脚本发送按键序列（分组发送，组间 sleep，让面板有机会绘制）；
3. 把完整 stdout 字节流落盘，再离线解析成逐帧文本。

```bash
python scripts/probe_ink_frames.py --keys "nnndadadad|\r|\x1b" --wait 7
python scripts/probe_ink_frames.py --grep "working"      # 只在帧里找目标文字
```

产出：

```
temp/ink_frames/<stamp>/stdout.bin    # 原始字节，ANSI 完整
temp/ink_frames/<stamp>/frames.txt    # 去 ANSI、按屏拆分，可直接 grep
temp/ink_frames/<stamp>/stderr.txt
```

关键点：**帧边界靠 erase 序列切分，不能按换行切**（一帧里有很多换行）。脚本用连续空行 + ANSI 擦除做分隔。

这个方法的适用面很广：任何「一闪而过」「来不及看」「偶发」的终端 UI 问题，都能用它把一次性现象变成可反复检查的文件。

### 2.2b 挂机模式：用户操作，我来记录（`--attach`）

2.2 的驱动模式有个前提——**你得能自己复现**。当 bug 依赖活的 agent-team / workflow 上下文，而这个上下文只有用户的真实会话才有时，驱动模式抓不到东西（本次就撞了这堵墙：抓了 29 帧，全是 `idle`）。

挂机模式反过来：用户在**自己的终端**里正常跑 `ga` 并复现，我们只负责记录。关键是**不改 UI 源码**——`ga` 跑的是安装版，改源码对它无效，重装又太重。

做法是 Node 的 `--import` 预加载（`scripts/ga_frame_trace.mjs`）：在 `main.tsx` 执行前接管 `process.stdout`，把每次 write 按**字节长度定帧**追加到文件。与 UI 版本完全解耦。

```bash
# 一个终端：起记录器，它会把下面那行命令打印出来
python scripts/probe_ink_frames.py --attach --grep Waiting --idle-exit 120

# 另一个终端：粘贴它打印的那行（PowerShell 示例）
$env:GA_UI_TRACE="D:\...\temp\ink_trace"; `
  $env:NODE_OPTIONS='--import "file:///D:/.../scripts/ga_frame_trace.mjs"'; ga
```

复现完退出 ga，第一个终端会把帧汇总打出来。产物 `temp/ink_trace/frames-<pid>.log`。

两个坑，都是实测踩出来的：

- **`NODE_OPTIONS` 会传染给所有子 node 进程**（MCP server、npm 助手、workflow 校验）。第一版没过滤，一次运行产出 11 份 trace，真正的 UI 淹在噪音里。现在默认只记录 argv 里含 `main.tsx` 的进程（`GA_UI_TRACE_ALL=1` 可放开）。
- **必须用 Proxy 包裹 stdout，不能替换成普通对象**。ink 要读 `isTTY` / `columns` / `on('resize')`；换成普通对象会把 ink 切到非 TTY 渲染路径，录下来的帧和用户实际看到的**根本不是同一个东西**。

### 2.3 用真实数据复刻后端 payload

前端可疑时，先确认后端到底发出了什么。不要读代码猜，直接把 bridge 的组包逻辑跑一遍：

```python
from genericagent import paths as gp
from genericagent.agents.team import SubagentTeam, team_name_for
from genericagent.agents.registry import SubagentRegistry
from genericagent.agents.tasks import SubagentTaskStore

sid = '<session id>'
roots = gp.project_roots(project=r'D:\git_codes\GenericAgent',
                         data_root=r'C:\Users\drago\AppData\Local\GenericAgent')
team = SubagentTeam(SubagentRegistry(roots.project_data_dir/'subagents'),
                    '/root', team_name=team_name_for(sid), session_id=sid)
print([m['name'] for m in team.view()['members']])
```

本次实测：该 session 的 registry 只有 3 条 `verify_*` 记录，**没有**界面里显示的 `impl-b` / `impl-c`。这一步直接证伪了「界面数据来自 team roster」的假设，省下了后面大量的错误方向。

### 2.4 把候选字符串在「正在运行的那份代码」里搜一遍

顺序很重要：先 `2.1` 定位实际运行目录，再去那个目录搜。搜索要覆盖非常规位置：

- `site-packages/frontends/`、`site-packages/genericagent/`
- 旧 venv 副本（本仓库 `temp/release-venv-*`、`temp/wheel-inspect-*`、`temp/_wheel_check`）
- `temp/model_responses/*.txt`（bridge 记录的模型请求/响应）
- 主会话转录 `projects/<key>/session_<id>.jsonl`
- `.git` 全历史：`git log --all -S"<字符串>"`

---

## 3. 失败路径（同样重要）

以下方法本次都用了，且都没有定位到问题。记录下来是为了后来者不再重复。

| 做法 | 结果 | 为什么无效 |
| --- | --- | --- |
| 在源码树里全量 grep 目标文字 | 零命中 | 跑的是 site-packages，源码树里本就没有（见 2.1） |
| 搜 git 全历史 `-S` | 零命中 | 那套文案从未进入本仓库 |
| 搜 GA 全部数据文件（jsonl/json/log） | 只命中我自己写进测试的 docstring | 目标文字不在 GA 产出里 |
| 猜「是 team 面板」并直接改 `SubagentTeam.members()` | 改了、测了、合并了，但**没解决问题** | 假设未验证就先动手，见教训 4.1 |
| 按时间戳/命名格式反推来源 | 该 ID 形态与代码里唯一的生成器不一致 | 命名格式不是稳定的身份，见教训 4.2 |
| 手工截图对照布局 | 两次截图都只截了中间部分 | 缺的正是最能定位身份的顶栏/底栏 |

---

## 4. 教训

### 4.1 先验证假设，再改代码

本次最大的失误：看到「team 面板里混入 workflow 子代理」，就直接认定数据源是 `SubagentTeam.members()`，写测试、改实现、跑回归、提交、打包安装，一整套做完之后才发现**那条路径根本不产出界面里那些行**。

正确顺序应该是：先用 2.3 的方法把真实 payload 打出来，确认界面内容确实来自它，再动手。

那个修复本身是合理的加固（`members()` 确实不做归属过滤，是真实缺陷），但它不是用户报的问题。**修对了东西，修错了问题** —— 这种失误比改错代码更隐蔽，因为测试全绿、日志正常，只有回到用户现象才能发现没解决。

### 4.2 命名格式不是身份

`wf_1778661927543-21c-1` 这个形态让人一路追错方向：先当成 workflow runId，再当成 worktree 名，再当成时间戳。

- 代码里 `new_run_id()` 产出的是 `wf_<32位hex>`（UUID）；
- 真实的 `.claude/worktrees/` 里确实有 `wf_1778347f-21a-3` 这种形态；
- 但 GA 自己的 worktree 是 `run_000090`。

同一个前缀 `wf_` 被至少两套命名方案使用。**不要从字符串形态推断来源**，要去数据里找那条记录本身。

### 4.3 用户描述的操作序列是重要线索，但要精确复述

「打 /wokflows 然后立刻按 ESC」→ 后续澄清是「不是 workflows」→ 最终是「任意乱码 + 回车 + ESC」。

拼错/记错命令名很常见。收到复现步骤后，**先用它跑一次**（`probe_ink_frames.py --keys`），用结果对齐认知，而不是照字面去读 `trimmed === '/workflows'` 那条分支。字面读代码会把人锁死在错误的分支上。

### 4.4 搜索要先定位「谁在跑」

`2.1` 那一步只需 30 秒，能省掉前面几轮全部无效搜索。本仓库特别容易踩：`ga` 是安装版入口，源码目录只是 cwd。

**任何「代码里搜不到」的情况，第一反应应该是「我搜的是不是正在运行的那份」。**

---

## 5. 下次遇到同类问题的推荐顺序

1. **定位运行体**：`psutil` 看 cmdline/cwd（2.1）。确认是源码还是 site-packages。
2. **判断能不能自己复现**。
   - 能 → 驱动模式（2.2）：`--keys` 打用户的按键序列。
   - 不能（依赖真实会话上下文）→ **直接上挂机模式（2.2b）**，不要在空会话里抓半天。本次的教训：驱动模式抓到 29 帧全是 `idle`，浪费了一轮。
3. **在帧里搜**：`--grep <用户看到的文字>`。命中就拿到确切文案与上下文；零命中说明文字不是这份代码产的。
4. **复刻后端 payload**：用真实数据跑一遍 bridge 的组包逻辑（2.3），确认界面内容的数据来源。
5. **只有 2/3/4 都指向同一处时，才动代码。**

第 5 步之前任何一步缺失，都可能重演 4.1 的失误。

---

## 6. 相关文件

- `scripts/probe_ink_frames.py` —— 2.2 驱动模式 + 2.2b 挂机模式的实现
- `scripts/ga_frame_trace.mjs` —— 2.2b 的 `--import` 预加载记录器（零侵入 stdout tee）
- `tests/test_probe_ink_frames.py` —— 按键解码 / 分帧 / trace 解析 / 预加载不变量
- `docs/ga_ink_ui_testing_playbook_2026-07-16.md` —— Ink UI 测试总纲（纯函数、虚拟终端、字节断言等），本文是它在「瞬时帧」场景下的补充
- `tests/test_subagent_team.py` —— 本次顺带补的 registry 归属过滤回归