# Ink 瞬时帧调试方法（2026-11-11）

> 本文记录一次真实的排查全过程：用户报「agent team 执行中，界面自动弹出几行上一轮
> workflow 的 subagent 播报，一闪而过 / 长时间挂着」。
>
> 排查**第一轮失败了**（猜错数据源，改了无关代码就打包），第二轮靠抓帧收敛。
> 两轮都在本文里，因为**失败路径和成功路径同等重要** —— 本文的实用价值主要来自
> 「什么时候不该动代码」。
>
> 适用面不限于 GA：任何「一闪而过」「来不及看」「偶发」的终端 UI 问题都适用。

---

## 0. 一句话方法论

> **把一次性现象变成磁盘上可反复检查的文件，再去比对时间线。**

人眼读不完一帧，但文件可以 grep 一万遍。所有后面的步骤都是这句话的展开。

---

## 1. 问题特征

用户描述的现象，决定了必须用什么方法：

- 面板/文字**一闪而过**，肉眼读不完、截图来不及；
- 能被一段**确定的按键序列**触发（这里是：任意乱码 → 快速回车 → 快速 Esc）；
- 显示的内容与真实数据吻合（确实是某次 workflow 的子代理），但渲染代码在仓库里搜不到。

这类 bug 的本质：

> **bug 存在于跨多次 render 的状态时序里**，而非某个纯函数的输出里。

推论：纯函数单测看不见它；打字事件驱动的集成测试能不能看见取决于时序；**人眼基本无望**。

---

## 2. 工具：两个模式，一件必须做的事

### 2.1 先确认「谁在跑，再读代码」（必须做，30 秒）

**这是整个流程里性价比最高的一步，也是最容易漏的一步。**

本仓库 `ga` 走 `C:\anaconda3\Scripts\ga.exe` → **site-packages 里的 wheel**，不是当前
目录的源码。所以「在源码目录执行 ga」**不等于**「跑的是源码」。

```bash
python -c "
import psutil
for p in psutil.process_iter(['pid','cmdline','cwd']):
    try:
        j = ' '.join(p.info['cmdline'] or [])
        if 'psutil' in j: continue
        if any(k in j for k in ('main.tsx','ink','ga_cli','genericagent.core.app','bridge.py')):
            print(p.info['pid'], p.info['cwd'])
            print('  ', j[:200])
    except Exception: pass
"
```

实测输出：

```
PID 29984  cwd: C:\anaconda3\Lib\site-packages\frontends\ink\ui
           node ...tsx/dist/cli.mjs ...\frontends/ink/ui/src/main.tsx
PID 37136  cwd: C:\anaconda3\Lib\site-packages\frontends\ink\ui
           python ...\frontends/ink/bridge.py
```

顺带能拿到进程环境，判断它读的是哪份配置、哪个数据根：

```python
env = psutil.Process(pid).environ()
for k in ('GA_PROJECT_ROOT','GA_DATA_DIR','GA_LLM_CONFIG','GA_TEAM_NAME'):
    print(k, env.get(k))
```

> **规则：任何「代码里搜不到用户描述的文字」的情况，第一反应都应该是
> 「我搜的是不是正在运行的那份」。**

### 2.2 驱动模式：自己按键，脚本记录

前提是**你能自己复现**。

```bash
python scripts/probe_ink_frames.py --keys "nnndadadad|\r|\x1b" --wait 7
python scripts/probe_ink_frames.py --grep "用户看到的文字"
```

脚本做三件事：用**和 `ga` 完全相同的入口**启动（`tsx src/main.tsx`，cwd 设为 UI 目录）；
按组发送按键（组间 sleep，让面板有机会绘制）；把完整 stdout 字节流落盘。

```
temp/ink_frames/<stamp>/stdout.bin    # 原始字节，ANSI 完整
temp/ink_frames/<stamp>/frames.txt    # 去 ANSI、按屏拆分，可直接 grep
temp/ink_frames/<stamp>/stderr.txt
```

**关键点：帧边界不能按换行切**（一帧里有很多换行），要靠 ANSI erase 序列 / 连续空行。

### 2.3 挂机模式：用户操作，我来记录（`--attach`）

驱动模式有个硬前提：**你得能自己复现**。本次就撞了这堵墙 —— 那个浮层依赖活的
agent-team / workflow 上下文，空会话里根本不存在。抓了 29 帧，**全是 `idle`**，白跑一轮。

挂机模式反过来：用户在**自己的终端**里正常跑 `ga` 复现，我们只负责记录。

难点在于 `ga` 跑的是安装版：改源码对它无效，重装 wheel 又太重、会打断用户正在进行的工作。
解决办法是 Node 的 `--import` 预加载（`scripts/ga_frame_trace.mjs`）：

```
ga.exe → python bridge.py → node tsx cli.mjs → main.tsx
                                                    ↑ 在这里，main.tsx 执行前接管 stdout
```

```bash
# 终端 A：起记录器，它会打印下面那行命令
python scripts/probe_ink_frames.py --attach --grep "Waiting" --idle-exit 180

# 终端 B：粘贴它打印的那行（PowerShell）
$env:GA_UI_TRACE="D:\...\temp\ink_trace"
$env:NODE_OPTIONS='--import "file:///D:/.../scripts/ga_frame_trace.mjs"'
ga
```

用户正常复现，退出后终端 A 自动汇总。与 UI 版本**完全解耦**，任意版本、任意目录都能用。

#### 三个实测踩出来的坑（都是会让结果无声失效的那种）

1. **`NODE_OPTIONS` 会传染给所有子 node 进程** —— MCP server、npm 助手、workflow 校验
   全都继承。第一版没过滤，一次运行产出 **11 份** trace，真正的 UI 淹在噪音里，看起来
   像"抓了一堆但没抓到"。现在默认只记录 argv 含 `main.tsx` 的进程（`GA_UI_TRACE_ALL=1` 放开）。

2. **必须用 Proxy 包裹 stdout，不能换成普通对象**。ink 要读 `isTTY` / `columns` /
   `on('resize')` / `getColorDepth`；换成普通对象会把 ink 切到**非 TTY 渲染路径**，
   录下来的帧和用户实际看到的**根本不是同一个东西**。这是「观测改变了被观测对象」的
   经典形态 —— 最坏情况是它会让你对着一个不存在的 bug 调半天。

3. **必须按字节长度定帧，不能按空行切分**。chunk 本身就含大量换行与 ANSI 序列，
   空行切会把一屏切成碎片。trace 格式是长度前缀：

   ```
   \n=== GA_FRAME <iso> #<n> cols=<w> rows=<h> bytes=<b> ===\n
   <恰好 b 个字节的原始 chunk>
   ```

   解析时必须**按字节扫**，不能先 decode 成 str 再 regex —— `bytes=N` 是字节数，一旦
   chunk 含中文（每个 label 都是），字符偏移和字节偏移就开始错位，后面所有帧全废。

---

## 3. 从帧里定位问题（核心方法论）

拿到 trace 之后的四步。**按顺序做，每一步都可能直接结案。**

### 3.1 先按用户描述的文字 grep

```python
frames = read_trace_file(Path('temp/ink_trace/frames-23960.log'))
for kw in ['Waiting', 'impl-b', 'working', 'wf_', 'Lead']:
    hits = [f['seq'] for f in frames if kw in f['raw']]
    print(f'{kw!r}: {len(hits)} 帧', hits[:12])
```

本次结果：

```
'Waiting': 40 帧 [572, 573, ...]
'wf_':     175 帧 [158, 161, ...]
'working': 0 帧
'impl-b':  0 帧
```

**零命中同样是信息**：说明用户记的字面文字不是这份代码产的（很常见，用户复述会变形）。
`Waiting` 命中 40 帧就直接拿到了确切文案与上下文。

### 3.2 画出现/消失时间线（本次的关键一步）

不要只看「第一帧命中」，要看**连续的区块**。忽略纯光标 park 写入（剥掉 ANSI 后为空的帧），
把「含目标文字 / 不含」压成 run：

```python
runs = []; cur = None
for f in frames:
    if not strip_ansi(f['raw']).strip():   # 纯 cursor-park 写入，跳过
        continue
    has = 'workflow-child' in f['raw']
    if cur and cur[0] == has: cur[2] = f['seq']; cur[3] = f['iso']
    else: cur = [has, f['seq'], f['seq'], f['iso']]; runs.append(cur)
```

本次输出（这就是结案证据）：

```
False seq   5..354   13:59:40
True  seq 357..357   13:59:40   ← workflow 跑完，播报 5 条
False seq 360..534   14:00:38   ← 回合结束，播报被清空
True  seq 537..785   14:04:06   ← 下一个回合（agent team）开始，又回来了
```

**「消失后又出现」这个形态本身就是答案**，它排除了大量假设：

- 不是「一直没清掉」（那会是一个连续 run）
- 不是「第一次画错」（那不会再出现）
- 而是**某个"清空"之后又发生了"重新播报"**

顺着这条线，问题立刻从「谁渲染了这些行」变成「**为什么清空之后会重播**」。后者可以精确回答。

### 3.3 dump 转折帧，看它和前后帧的差别

```python
for seq in (356, 357, 358):
    f = frames_by_seq[seq]
    print(m.strip_ansi(f['raw']))
```

本次帧 537 的内容就是用户截图里那些行：

```
 ✓ Subagent workflow-child:wf_6dcbb054...:agent_1 completed
 ✓ Subagent workflow-child:wf_6dcbb054...:agent_2 completed
 ✓ Subagent workflow-child:wf_6dcbb054...:agent_3 completed
 ✓ Subagent workflow-child:wf_6dcbb054...:agent_4 completed
 ✓ Subagent workflow-run:wf_6dcbb054... completed
```

同时帧 537 里还有一个关键旁证：输入框是 `> /agent`，`Team session-... · 1 member(s)` ——
说明当前回合是 **agent team**，而播报内容属于**上一轮的 workflow**。两者对不上，就是这个 bug。

### 3.4 复刻后端 payload 对账

有了确切文案，去问「后端到底发了什么」，**不要读代码猜**：

```python
from genericagent import paths as gp
from genericagent.workflow.store import WorkflowStore
from genericagent.workflow.controller import WorkflowController
from genericagent.core.control.workflow import WorkflowChildAdapter

roots = gp.project_roots(project=r'D:\git_codes\GenericAgent',
                         data_root=r'C:\Users\drago\AppData\Local\GenericAgent')
store = WorkflowStore(root=roots.project_data_dir,
                      project_workflows_dir=roots.project_workflows_dir,
                      user_workflows_dir=roots.user_workflows_dir,
                      legacy_workflows_dir=roots.legacy_workflows_dir)
adapter = WorkflowChildAdapter(store, WorkflowController(store=store),
                               session_id_provider=lambda: 'session_<id>')
for r in adapter.list_records(include_terminal=True):
    print(r.execution_id, r.status, r.source_status)
```

本次输出与界面文字**逐字对得上**（5 条，含 `agent_1..4` + run 记录）。
到这一步才敢动代码 —— 判据是：**帧里的文字 == 复刻 payload 的 output**。

> 注意 `session_id_provider` 要传 `session_<32hex>` 完整形态；只传裸 hex 会返回 0 条记录，
> 很容易误判成"数据源不对"。

---

## 4. 根因：一类可复用的 bug 模式

本次根因是两处代码事实撞在一起，而它们各自单独看都**完全合理**。

**后端**（`bridge.py:_emit_agent_snapshot`）：

```python
records = self.agent_control.list_records(include_terminal=True)
```

语义是「会话 workflow store 里所有终态记录」，**每次快照整份重发**。这是正确的读模型 ——
团队面板、workflow 面板都需要看全部历史。问题不在它。

**前端**（修复前 `App.tsx`）：

```ts
const byExecution = new Map<string, AgentNotice>()
for (const notice of agentNoticesRef.current) byExecution.set(notice.executionId, notice)
```

它把「当前显示的 notices」当作去重表。而 `agentNotices` 在**回合结束时被清空**。

时序：

```
回合1 · workflow
  快照到达 → 去重表空 → 5 条都是"新的" → 播报 ✓   (帧 357)
回合1 结束
  setAgentNotices([])                              ← 记忆跟着显示一起没了
回合2 · agent team
  快照到达 → 去重表又空 → 那 5 条又成"新的" → 重播  (帧 537)
```

### 模式命名

> **把「全量状态快照」当成「增量事件流」来消费。**

事件流可以「收到就播报」；全量快照不行 —— 它每次都在陈述「这些记录**存在**」，
而不是「这些记录**刚刚发生**」。任何用全量快照驱动的通知/播报，都必须自带一个
**跨快照存活的幂等集合**，而不能从当前显示状态反推。

这个模式在 GA 里已经出现过一次（`docs/ga_ink_ui_testing_playbook_2026-07-16.md` 记的
team roster 过滤也是同族：消费方按自己的假设重新推导，而不是问唯一权威来源）。

### 这也解释了现象的两种形态

| 用户看到 | 为什么 |
| --- | --- |
| **一闪而过**（乱码+回车+Esc） | 回合瞬间开始→重播→用户按 Esc 结束回合→清空。所以只闪一下 |
| **长时间挂着**（team 回合） | team 回合跑几分钟，期间清空逻辑不触发（只在回合结束跑），脏数据一直留在屏上 |

同一个 bug，两种观感。**如果只按「一闪而过」去查，会误以为是个渲染时序问题、
去查节流/帧合并** —— 那是另一个方向。

---

## 5. 修复：把两个概念拆开

它们本来就该是两个变量，只是过去恰好是同一个：

| 变量 | 含义 | 生命周期 |
| --- | --- | --- |
| `notices` | 此刻屏幕上画什么 | 回合结束清空 |
| `announced` | 用户**曾经**被告知过哪些 id | 跨回合存活，只在 `/new` 时清 |

```ts
const announcedAgentsRef = useRef<Set<string>>(new Set())

// 不在屏幕上 + 曾经播报过 → 保持沉默
if (announcedNext.has(notice.executionId)) continue
```

**记忆必须比它所描述的那个显示活得更久。** 这是修复的全部内容。

### 两个特意没做的选择

**没有去限制后端只发当前回合的记录。** 那份全量快照同时喂给团队面板、workflow 面板，
它们需要看全部历史。收窄后端会顺带弄坏别的视图。**全量读模型是对的，错的是前端拿它当事件流。**

**去重键用 `executionId`，不是 `executionId + verdict`。** 子代理状态会正常演进
（`running` → `closed` → `succeeded`）。用 verdict 做键会把一次演进当成三个新子代理，
留下 `✗ failed` 和 `✓ completed` 并排（这是更早修过的另一个 bug）。
代价是：**已在屏幕上**的行仍允许原地更新 verdict，不会被卡在过期结论上。

---

## 6. 验证：三重，缺一不可

### 6.1 回放真实 trace 时间线

用**用户机器上那份真实 store**（不是手写 fixture）跑一遍合并逻辑，重现抓帧时间线：

```
回合1（workflow 跑完）播报: 5 条     ← 与帧 357 完全一致
回合2（agent team）播报: 0 条        ← 修复目标
```

### 6.2 变异测试：证明测试真的在测那个修复

把修复那一行去掉，**确认对应用例失败**：

```
✖ a record announced in an earlier turn is not announced again after a clear
✔ 其余 4 例通过
```

不做这一步的测试，很容易是「恒真」的 —— 全绿但什么都不守。本次的做法是临时删掉
`if (announcedNext.has(...)) continue` 再跑。

### 6.3 在**安装版**上验证，而不是源码树

`ga` 跑的是 site-packages。构建 wheel 后要确认：

1. 修复代码**确实在 wheel 里**（`zipfile` 读出来查关键符号）；
2. 装完之后直接 import **site-packages 里的模块**跑 6.1 的回放。

只检查「文件已经 copy 进去了」不够 —— 要跑的是那份代码。

---

## 7. 失败路径（第一轮，同样重要）

| 做法 | 结果 | 为什么无效 |
| --- | --- | --- |
| 在源码树里全量 grep 目标文字 | 零命中 | 跑的是 site-packages（§2.1） |
| 搜 git 全历史 `git log --all -S` | 零命中 | 那套文案从未进入本仓库 |
| 搜 GA 全部数据文件（jsonl/json/log） | 只命中我自己写进测试的 docstring | 目标文字不在 GA 产出里 |
| **猜「是 team 面板」并直接改 `SubagentTeam.members()`** | 改了、测了、合并了、**打包了**，但没解决问题 | 假设未验证就动手，见 8.1 |
| 按时间戳/命名格式反推来源 | 该 ID 形态与代码里唯一的生成器不一致 | 命名格式不是身份，见 8.2 |
| 手工截图对照布局 | 两次截图都只截了中间部分 | 缺的正是最能定位身份的顶栏/底栏 |
| 在空会话里跑驱动模式 | 29 帧全是 `idle` | 依赖的上下文不存在，见 §2.3 |

---

## 8. 教训

### 8.1 先验证假设，再改代码 —— 这是本次最贵的教训

第一轮看到「team 面板里混入 workflow 子代理」，就直接认定数据源是 `SubagentTeam.members()`，
写测试、改实现、跑回归、提交、**打包安装**，一整套做完才发现**那条路径根本不产出界面里那些行**。

那个修复本身是合理的加固（`members()` 确实不做归属过滤，是真实缺陷），但它**不是用户报的
问题**。这种失误比改错代码更隐蔽：

- 测试全绿
- 日志正常
- 提交信息合理
- 只有回到用户现象才能发现没解决

**修对了东西，修错了问题。** 正确顺序是先按 §3.4 把真实 payload 打出来，确认界面内容
确实来自它，再动手。

### 8.2 命名格式不是身份

`wf_1778661927543-21c-1` 这个形态让人一路追错：先当 workflow runId，再当 worktree 名，
再当时间戳。而实际上同一个 `wf_` 前缀被至少两套命名方案使用（`new_run_id()` 产
`wf_<32hex>`；`.claude/worktrees/` 里有 `wf_1778347f-21a-3`；GA 自己的 worktree 是
`run_000090`）。

**不要从字符串形态推断来源**，要去数据里找那条记录本身。

### 8.3 用户给的复现步骤要「跑」，不要「读」

「打 /wokflows 然后立刻按 ESC」→ 澄清「不是 workflows」→ 最终「任意乱码 + 回车 + ESC」。

拼错/记错命令名很常见。收到复现步骤后**先跑一次**，用结果对齐认知，而不是照字面去读
`trimmed === '/workflows'` 那条分支 —— 字面读代码会把人锁死在错误分支上。

### 8.4 「一闪而过」不等于「渲染时序问题」

这个 bug 的两种观感（闪一下 / 长时间挂着）源于同一个数据问题。**先抓帧确认它「存在多久」，
再决定往"渲染节流/帧合并"查，还是往"数据为什么出现在这里"查。** 本次第一轮的直觉
（一闪而过 → 渲染时序）是错的。

### 8.5 测试要能被证伪

新增的用例必须**在去掉修复时失败**。变异测试（§6.2）是唯一能证明这件事的廉价手段。
不做的话，很容易写出「恒真断言」—— 全绿、但什么都不守。

---

## 9. 下次遇到同类问题的推荐顺序

1. **定位运行体**（§2.1，30 秒）。确认源码还是 site-packages。
2. **判断能不能自己复现**。
   - 能 → 驱动模式（§2.2）。
   - 不能 → **直接上挂机模式**（§2.3），别在空会话里白抓（本次浪费一轮）。
3. **grep 帧里的用户描述文字**（§3.1）。零命中也是信息。
4. **画出现/消失时间线**（§3.2）。「消失后又出现」直接锁定问题类型。
5. **dump 转折帧**（§3.3），记下同时刻的其他 UI 状态（当前回合是什么、输入框内容）。
6. **复刻后端 payload 对账**（§3.4）。判据：帧里的文字 == 复刻 payload 的 output。
7. **只有 3~6 都指向同一处时，才动代码。**
8. 修完做**三重验证**（§6），并**重新打包安装版**后再验一次。

第 7 步之前任何一步缺失，都可能重演 8.1 的失误。

---

## 10. 相关文件

- `scripts/probe_ink_frames.py` —— §2.2 驱动模式 + §2.3 挂机模式的实现；含 trace 解析器
- `scripts/ga_frame_trace.mjs` —— §2.3 的 `--import` 预加载记录器（零侵入 stdout tee）
- `tests/test_probe_ink_frames.py` —— 按键解码 / 分帧 / trace 解析 / 预加载不变量
- `frontends/ink/ui/src/agentNotice.ts` —— `mergeAgentNotices`：本次修复的落点
- `frontends/ink/ui/src/subagentNoticeReannounce.test.ts` —— §6.1/§6.2 对应的回归用例
- `docs/ga_ink_ui_testing_playbook_2026-07-16.md` —— Ink UI 测试总纲（纯函数、虚拟终端、
  字节断言等）。本文是它在「瞬时帧 / 一次性现象」场景下的补充
- `tests/test_subagent_team.py` —— 第一轮顺带补的 registry 归属过滤回归（真实缺陷，
  但与本现象无关）