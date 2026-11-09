# 从一次 `ga_spawn` 看懂 ACP：GenericAgent 当 Server，Hlaudz 当 Client

> 配套图：
> [`ga-acp-architecture.html`](./ga-acp-architecture.html)（组件与边界）
> · [`ga-acp-sequence.html`](./ga-acp-sequence.html)（一次完整往返的时序）
>
> 代码版本：GenericAgent `694820e`、inner_chatgpt / Hlaudz `de7e71c`。
> 文中所有 `path:line` 都对应这两个版本；GitHub 链接也钉在 commit 上。

---

## 0. 一句话版本

**ACP 就是一条 stdio 上的 JSON-RPC 2.0 会话。**
GenericAgent 的 `frontends/acp/genericagent_acp_bridge.py` 是 **Agent / Server** 端；
Hlaudz（inner_chatgpt）的 `backend/app/acp/` 是 **Client** 端。

Hlaudz 的主 Agent 通过 5 个工具（`ga_spawn` / `ga_wait` / `ga_result` / `ga_send` / `ga_stop`）
把子任务交给一个**独立的 GA 子进程**；GA 用自己的模型、自己的回合循环干活，
通过 `session/update` 把文本和工具事件流回来，Hlaudz 再翻译成已有的「子代理卡片」。

记住这一条，后面全是推论：

> **一次 GA 会话 = 一个 GA 子进程 = 一条 ACP 连接。**
> 不是线程，不是协程，也不是同进程里的函数调用。

---

## 1. 先补最小的 ACP 协议课

ACP（Agent Client Protocol）没有自定义二进制帧，也没有 HTTP。
它规定：**一行一个 UTF-8 JSON**，跑在子进程的 stdin / stdout 上。

### 1.1 三种报文

| 方向 | 形态 | 例子 |
| --- | --- | --- |
| Client → Agent | 请求 | `initialize`、`session/new`、`session/prompt`、`session/close` |
| Client → Agent | 通知（无 id） | `session/cancel` |
| Agent → Client | 通知 | `session/update`（文本增量、工具调用） |
| Agent → Client | 请求 | `session/request_permission` |
| 双方 | 响应 | 带同一个 `id` 的 `result` 或 `error` |

关键点：**请求和响应靠 `id` 关联**，通知没有 `id`。
所以客户端必须维护一张 `id -> Future` 的表；服务端必须按行解析、按 `id` 回包。

### 1.2 一次最小往返长什么样

下面把真实的报文压成几行（`>>` 是 Client 发给 Agent，`<<` 是 Agent 回给 Client）：

```text
>> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,...}}
<< {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentInfo":{...},...}}

>> {"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"D:/.../workspace","mcpServers":[]}}
<< {"jsonrpc":"2.0","id":2,"result":{"sessionId":"ga_ab12...","modes":{...}}}

>> {"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"ga_ab12...","prompt":[{"type":"text","text":"计算 2+3"}]}}
<< {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"ga_ab12...","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"2 + 3 = 5"}}}}
<< {"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}
```

`session/prompt` 是**一个会等待的请求**：Agent 只在整轮结束时才回它的 `result`；
中间的过程全部通过 `session/update` 通知流回来。这是理解流式卡片的关键。

### 1.3 先看图

- 架构图看「谁在哪个进程、哪条边界、两套模型怎么分开」；
- 时序图看「一次 `ga_spawn` 到 `ga_result` 到底发了哪些报文」。

看完图再读下面的代码，会容易很多。

---

## 2. GA 侧：Bridge 怎么把 ACP 接到 `GeneraticAgent`

入口文件：

```text
frontends/acp/genericagent_acp_bridge.py
```

它只做四件事：**守好 stdout、解析请求、把任务塞给 GA、把 GA 的事件翻译成 ACP 通知。**

### 2.1 第一件事：把 stdout 变成私有通道

Bridge 在 import `genericagent.core.app` **之前**就先处理 stdout
（[bridge:14](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L14)）：

- 复制一份原始 stdout 的 fd，得到 `_acp_stdout`，专门写 JSON-RPC；
- 把原来的 fd 1 重定向到 stderr；
- 再把 `sys.stdout` 换成一个「写到哪里都转 stderr」的 router。

为什么必须这么早？因为 `genericagent.core.app` 及其子模块在 import / 初始化时可能 `print()`。
如果这些字进了 stdout，客户端读到的就不是 JSON，整条协议直接坏掉。
同理，GA 的工具会启动**子进程**；如果 fd 1 可继承，子进程的日志也会污染 ACP 通道，
所以 Windows 分支还调用了 `os.set_inheritable(_stdout_fd, False)`
（[bridge:24](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L23)）。

Windows 上还额外做了 `msvcrt.setmode(..., os.O_BINARY)`：
否则 stdin/stdout 会按文本模式做 CRLF 转换，JSON 行会被改坏。

> 这一个设计点，直接决定了 Hlaudz 的预检**不能 import bridge**，只能 `find_spec`。
> 见 §3.2，这是这套实现里最值得学的坑之一。

### 2.2 `initialize`：声明能力

[bridge:202](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L202)
返回：

```json
{
  "protocolVersion": 1,
  "agentCapabilities": {
    "loadSession": false,
    "mcpCapabilities": {"http": false, "sse": false},
    "promptCapabilities": {"image": false, "audio": false, "embeddedContext": false}
  },
  "agentInfo": {"name": "genericagent-acp", "title": "GenericAgent", "version": "0.1.0"}
}
```

`loadSession: false` 很重要：**GA 不支持跨连接恢复会话**。
所以 Hlaudz 的设计是「一轮 generation 一批 GA 会话，本轮结束全部回收」，
不承诺跨轮续跑 GA 的上下文。需要续跑时，由主 Agent 把必要信息重新写进任务书。

### 2.3 `session/new`：一个会话就是一个独立 Agent

[bridge:226](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L226)
要求 `cwd` 必须是**绝对路径且真实存在的目录**，然后：

```python
agent = GeneraticAgent(workspace_root=cwd)
threading.Thread(target=agent.run, daemon=True).start()
session = SessionState(session_id=f"ga_{uuid4().hex}", cwd=cwd, agent=agent)
```

三个细节：

1. `workspace_root=cwd` 把 GA 的文件工具和命令执行圈在这个目录里；Hlaudz 传的是
   **服务端自己算出来的用户 workspace**，不是模型传入的路径（见 §3.6）。
2. 每个 session 起一个 `agent.run` 线程；GA 的回合循环本身是阻塞式的。
3. 返回 `sessionId` 和三种权限模式：`read_only` / `ask` / `full_access`。

### 2.4 `session/prompt`：把 ACP 请求变成 GA 队列任务

[bridge:370](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L370)
是整条链路的发动机。它做了五步：

1. 校验 `sessionId` 和 `prompt` 数组；把 ACP 的 content blocks 转成纯文本
   （`content_blocks_to_text`，图片/资源只留占位说明）。
2. 用 `prompt_lock` 保证**同一会话同时只有一个 prompt**；已有在途 prompt 就回 `-32603`。
3. 起一个后台线程 `run_prompt`，在里面调用 `session.agent.put_task(prompt_text, source="acp")`。
4. `_drain_agent_queue` 从 GA 的队列里读事件：带 `next` 的字符串是增量，写成
   `session/update` 的 `agent_message_chunk`；读到 `done` 就结束。
5. 最后给最初的 `session/prompt` 请求回 `{"stopReason": "end_turn"}`。

为什么 `done` 只在「一个 chunk 都没发过」时才用？因为 GA 的 `done` 文本会做
`</summary>` 插入等后处理，偏移会变；已经流过增量时再补整段会重复。
这是流式文本处理的一个真实坑。

### 2.5 工具事件与权限请求

GA 的工具执行通过两个回调接进 ACP：

- `agent.tool_event_callback` → `_on_tool_event`
  （[bridge:330](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L330)）：
  `phase=start` 发 `tool_call`，`phase=end` 发 `tool_call_update`，
  成功是 `completed`，失败/超时/取消/待审批是 `failed`。
- `agent.permission_runtime.set_emit` → `_emit_permission`
  （[bridge:273](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L273)）：
  发 `session/request_permission`，带两个选项 `allow_once` / `reject_once`。

Bridge 会记住 `jsonrpc_id -> (session_id, ga_request_id)`；
客户端回的 `result.outcome.optionId` 以 `allow` 开头就 `accept`，否则 `deny`。
如果发权限请求本身失败，它会立刻 `runtime.resolve(ga_request_id, "deny")`，
绝不让 GA 永远等一个到不了的审批。

### 2.6 `cancel` / `close` / 退出

- `session/cancel`（通知，无 id）→ [bridge:469](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L469)：
  清掉该会话所有待审批请求，然后 `session.agent.abort()`。
- `session/close` → 从会话表移除，`agent.request_shutdown()`；失败则 `abort()`。
- `serve()`（[bridge:535](https://github.com/valkryhx/GenericAgent/blob/694820e/frontends/acp/genericagent_acp_bridge.py#L535)）
  逐行读 stdin；EOF 时对所有剩余会话 `request_shutdown()`，然后退出。

> 注意：`session/cancel` 只是「请求取消」，不是「保证进程已退出」。
> 真正的进程回收由客户端负责（见 §3.3 的 `kill_tree_now`）。

### 2.7 GA 侧代码索引

| 想了解 | 位置 |
| --- | --- |
| stdout / fd 保护、Windows 二进制模式 | `genericagent_acp_bridge.py:14` |
| `initialize` 能力声明 | `:202` |
| `session/new` 与 `GeneraticAgent` 启动 | `:226` |
| 回调绑定（工具 / 权限） | `:264` |
| 权限请求与应答 | `:273` |
| `tool_call` / `tool_call_update` | `:330` |
| `session/prompt` 与后台线程 | `:370` |
| 增量事件 `_drain_agent_queue` | `:425` |
| `session/cancel` / `session/close` / 分发 | `:469`、`:480` |
| stdio 主循环 | `:535` |

---

## 3. Hlaudz 侧：一个 ACP Client 的五层栈

Hlaudz 没有把 ACP 做成 Skill，而是做成**内置工具 + 后端集成**。
原因很简单：Skill 在本项目里只是提示词文本，不能启动进程、不能持有连接、不能回收子进程。
「让 GA 干活」本质上是**增加一种执行后端**，必须落在代码里。

客户端按职责拆成 5 层，目录在 `backend/app/acp/`：

```text
config.py     解析环境变量：命令、模式、配额、超时、环境白名单
transport.py  Popen + stdin/stdout/stderr 三条管道 + 读线程 + 杀进程树
client.py     JSON-RPC 关联、请求/通知/响应三分、自动应答权限
session.py    一条 ACP 连接的状态机 + GA 事件 → Hlaudz subagent_* 事件
manager.py    多会话管理、配额、ProcessLease、cwd、wait/result/stop
tools.py      5 个 ga_* 工具（模型看到的 schema 与描述）
```

### 3.1 为什么预检用 `find_spec`，而不是 import

Hlaudz 在注入 `ga_*` 工具前要做一次准入检查：
**GA 的 bridge 是否装在这个 Python 环境里？**
如果装错环境，就不要把半个会话交给模型。

看起来最简单的写法是：

```python
import frontends.acp.genericagent_acp_bridge  # ❌ 会踩坑
```

但 bridge 在 import 时就把 stdout 重定向到了 stderr（§2.1）。
在探测子进程里 import 它，stdout 就再也拿不到模块路径，**健康安装会被误判成不可用**。

正确写法在
[manager.py:162](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/manager.py#L162)：

```python
import importlib.util as u
s = u.find_spec("frontends.acp.genericagent_acp_bridge")
print(s.origin if s else "")
```

`find_spec` 只解析模块路径，**不执行目标模块**，所以不会触发 bridge 的 stdout 重定向。
这就是「预检」与「真正启动」必须分开的原因。

预检结果按命令缓存；失败结果做 30 秒负缓存，装好依赖后无需重启进程即可恢复。

### 3.2 配置：只给 GA 它真正需要的环境

`config.py` 把 ACP 的所有开关集中成 `AcpSettings`
（[config.py:49](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/config.py#L49)）。
最常用的几个：

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `INNER_ACP_GA_PYTHON` | GA 装在这个解释器里；自动拼 `-m frontends.acp.genericagent_acp_bridge` | 空（=不启用） |
| `INNER_ACP_GA_COMMAND` | 完全自定义命令（JSON 数组或 shlex 字符串） | 空 |
| `INNER_ACP_GA_LLM_NO` | 追加 `--llm-no <n>`，选 GA 的 `llm.yaml` profile | 空 |
| `INNER_ACP_MODE` | `full_access` / `ask` / `read_only` | `full_access` |
| `INNER_ACP_PERMISSION` | 自动权限应答：`allow` / `deny` | `allow` |
| `INNER_ACP_MAX_ACTIVE_USER` | 每用户同时进行的 GA prompt 数 | 4 |
| `INNER_ACP_MAX_ACTIVE_GLOBAL` | 全进程同时进行的 GA prompt 数 | 80 |
| `INNER_ACP_PROMPT_TIMEOUT_S` | 单次 prompt 超时 | 1800 |
| `INNER_ACP_ENV_ALLOW` | 额外透传给 GA 的环境变量白名单 | 空 |

子进程环境由 `build_child_env`（[config.py:174](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/config.py#L174)）构造：
只放系统必需变量（`PATH`、`APPDATA`、`LOCALAPPDATA`、代理等）+ 显式白名单。
**绝不会把 Hlaudz 的 `INNER_*`、数据库路径、主模型密钥传给 GA。**

这也解释了两套模型怎么共存：

- Hlaudz 主 Agent 用用户在「设置 -> 我的模型」里配置的模型；
- GA 用 `%APPDATA%/GenericAgent/llm.yaml`（或 `GA_CONFIG_DIR` / `GA_LLM_CONFIG`）；
- 两边各自计费，互不读取对方密钥，也不共享进程内状态。

### 3.3 Transport：Windows 上没有 asyncio subprocess 怎么办

`transport.py` 是最「脏」但最不能省的一层。

Hlaudz 的开发启动是 `uvicorn ... --reload`，Windows 上跑的是 `SelectorEventLoop`，
它**不支持 asyncio 的子进程 API**。所以不能用 `asyncio.create_subprocess_exec`。

这里的做法是：

1. 用同步 `subprocess.Popen` 起进程，`stdin/stdout/stderr` 都是二进制管道
   （[transport.py:206](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/transport.py#L206)）。
2. 给 stdout / stderr 各起一个**读线程**；读到一行就 `call_soon_threadsafe` 投回事件循环。
3. 把 `Popen` 包成 `AsyncProcHandle`（[transport.py:90](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/transport.py#L90)），
   `wait()` 用 `asyncio.to_thread(self._proc.wait)`，这样既有的 `ProcessLease` 可以 `await`。
4. stderr 进一个带锁的环形 `deque`；出错时把末 N 行附到错误文本里，
   否则模型只能看到「进程挂了」，看不到原因。

杀进程是平台相关的
（[transport.py:123](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/transport.py#L123)）：

- Windows：`taskkill /PID <pid> /T /F`，连同 GA 拉起的子进程一起杀；
- POSIX：`killpg`；因此 `Popen` 必须 `start_new_session=True`，
  否则 GA 和 Hlaudz 服务端在同一个进程组，`killpg` 会把服务端自己一起杀掉。

杀完还要轮询 `poll()` 直到 `Popen` 真正回收；`subprocess` 不会自己更新 `returncode`。
不轮询的话，调用方看到的 `returncode` 永远是 `None`，既判断不了「停掉了」，也会留僵尸。

### 3.4 Client：请求关联与三分发

`AcpClient`（[client.py:52](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/client.py#L52)）
维护三样东西：

- `_next_id`：自增请求 id；
- `_pending: id -> Future`：等响应的槽位；
- `_active_prompt`：同一连接同时只允许一个 prompt。

每个入站消息在 `_dispatch`
（[client.py:182](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/client.py#L182)）
里分三类：

| 判据 | 类型 | 处理 |
| --- | --- | --- |
| 有 `method` + 有 `id` | 请求 | 只认 `session/request_permission`；其它回 `-32601` |
| 有 `method` + 无 `id` | 通知 | 只认 `session/update`，交给 `session.py` |
| 无 `method` + 有 `id` | 响应 | 按 `id` 找到 Future，`set_result` / `set_exception` |

`_request` 用 `asyncio.wait_for(fut, timeout)` 等响应，并在 `finally` 里清槽位：
超时、取消、发送失败都不能留下悬空的 Future。

权限请求（[client.py:198](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/client.py#L198)）
是 Agent → Client 的**请求**，必须回。Hlaudz 的策略由 `INNER_ACP_PERMISSION` 决定：
`allow` 回 `allow_once`，否则回 `reject_once`。回调异常被吞掉，绝不阻塞应答。

### 3.5 Session：一条连接的状态机

`GaSession`（[session.py:81](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/session.py#L81)）
是「一条 ACP 连接 + 它当前 prompt 的状态」。

启动（[session.py:134](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/session.py#L134)）按固定顺序：

```text
AcpProcess.start()        起进程、开始抽干 stdout/stderr
AcpClient.start()         initialize
client.new_session(cwd)   session/new
client.set_mode(...)     仅当配置模式与 GA 当前模式不同
status = running
```

每次 `prompt()` 生成一个**卡片 id**：`f"{agent_id}#{prompt_seq}"`，
例如 `ga_67769141#1`。这个 id 会一路带到 SSE 和数据库，前端靠它把事件归到同一张卡。

事件映射（[session.py:329](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/session.py#L329)）：

| ACP `session/update` | Hlaudz 事件 | 卡片表现 |
| --- | --- | --- |
| `agent_message_chunk` | `subagent_event` + `text_delta` | 卡片正文增量 |
| `tool_call` | `subagent_event` + `tool_start` | 工具步骤开始 |
| `tool_call_update` | `subagent_event` + `tool_result` | 工具步骤结束/失败 |
| 权限请求 | 一对 `tool_start` / `tool_result` | 不留下转圈的步骤 |

`_finish_prompt()`（[session.py:465](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/session.py#L465)）
无论成功、失败、超时、取消都会发一条 `subagent_end`，且幂等。
有文本事件时它**不重复塞 `result`**（卡片时间线已经展示过），
只有全程没有文本时才用占位文本；最终答复由 `ga_result` 单独读。

超时处理（[session.py:524](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/session.py#L524)）
是 `cancel → 2 秒宽限 → kill_tree_now`，和用户停止共用同一套语义。

### 3.6 Manager：多会话、配额、租约、工作目录

`AcpSessionManager`（[manager.py:123](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/manager.py#L123)）
属于**一轮 generation**，本轮结束就 `close_all()`。

`spawn()`（[manager.py:256](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/manager.py#L256)）
的关键点是 **立刻返回**：

1. 预检 bridge；
2. 拿配额（`quota.acp_active`，每用户 4 / 全局 80，
   [quota.py:36](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/quota.py#L36)）；
3. 算 `cwd = user_workspace(user_id)`（[manager.py:579](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/manager.py#L579)、
   [workspace.py:42](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/workspace.py#L42)）；
4. `GaSession.start()`；
5. 注册 `ProcessLease`（[process_registry.py:13](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/process_registry.py#L13)）；
6. `asyncio.create_task(_run_prompt(...))`，然后返回 `{"agent_id": "ga_xxx", "status": "running"}`。

所以主 Agent 调用 `ga_spawn` 不会阻塞自己的回合；
它之后可以用 `ga_wait` 等，也可以先去干别的。

`wait()` 超时**只表示还在跑，不会杀进程**；
`result(close=true)` 才释放打开会话名额。
`ProcessLease` 让既有的 `/stop` 能越过 ACP 层直接找到 GA 的进程。

### 3.7 Tools：模型看到的 5 个工具

`build_acp_tools()`（[tools.py:62](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/acp/tools.py#L62)）
只导出 5 个：

| 工具 | 作用 | 关键约束 |
| --- | --- | --- |
| `ga_spawn` | 新建 GA 会话并后台派任务 | `task` 必填；空任务直接拒绝 |
| `ga_wait` | 等一个/全部会话 | 超时不会终止 GA；可传 `since_event_seq` 取增量 |
| `ga_result` | 读最终答复/全量文本 | `close=true` 释放名额；不能把 `completed` 降级 |
| `ga_send` | 对同一会话追加 prompt | 一次只能有一个进行中的 prompt |
| `ga_stop` | 停止一个/全部 | 先 `session/cancel`，2 秒后杀树 |

工具体本身**永不向上抛异常**：所有错误都转成 `[工具执行错误] ...` 文本，
因为模型需要读到错误并自我修正，而不是让整个 AgentLoop 崩掉。
布尔参数还做了宽进严出（`_as_bool`）：模型把 `"false"` 写成字符串时不能当 `True`，
否则 `close="false"` 会真的释放会话名额。

### 3.8 挂载：只有 root agent 拿得到

关键在 `backend/app/subagent/session.py:149`：
只有主对话的 `TurnRuntime` 才 `build_acp_tools()` 并 `merged.add(t)`。
子代理 / teammate 在别处装配自己的 registry，天然没有 `ga_*`。

这不是提示词里的「请不要递归」；是**工具 schema 里根本没有**。
模型看不到，谈不上调用。这样避免了「GA 再派 GA」的数量和成本爆炸。

### 3.9 接进 SSE 与卡片：为什么前端不用改协议

Hlaudz 原本就有子代理（subagent）的 `EventBuffer` 和 `subagent_start/_event/_end` 事件，
前端已有 `SubagentCard`。ACP 层没有另造一套，而是**复用**：

- `GaSession._publish` 往同一轮 `EventBuffer` 里放 `subagent_*`；
- `chat.py` 的 `_merged_events(..., extra=acp)` 把主 Agent 事件和 ACP 事件合并进同一条 SSE
  （[chat.py:47](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/routers/chat.py#L47)）；
- `chat.py` 把 `subagent_*` 落成 message `blocks` 里 `type="subagent"` 的卡片
  （`block["role"]="ga"`，`agent_id` 带 `#1`）；
- 前端 `SubagentCard` 只是多了一个 `ga → GA 外援` 的角色标签。

这样做的好处是：**ACP 是一次后端扩展，不是前端协议升级。**
前端不需要知道 ACP、JSON-RPC、stdio 的存在。

### 3.10 停止：先停 GA，再关 SSE

用户点「停止」时，`/stop` 的顺序（[chat.py:738](https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/backend/app/routers/chat.py#L738)）是：

```text
request_stop（协作标志）
→ stop_acp_generation（最多 2s 优雅停止：session/cancel 等 GA 收尾）
→ cancel_generation（取消 SSE 流）
→ stop_subagent_generation
→ stop_code_runs
```

`stop_acp_generation` 能找到 manager，是因为本轮构建完 `TurnRuntime` 后
`chat.py:30` 的 `_register_acp_manager` 把它登记进了 `RuntimeHub`。
少了这一步，`/stop` 的优雅 ACP 停止就是空操作，只剩强杀。

SSE 关闭本身**不会**让 GA 失去停止指令：`_event_stream` 的 `finally` 还会走
`_cleanup_turn`，同步执行 `acp.stop_all_now()`（`mark_stopped` + `kill_tree_now`），
再异步 `close_all()`。

这里有两个容易踩的取消语义：

1. 流任务被取消后，**任何新的 `await` 都会立刻再抛 `CancelledError`**。
   所以终态写库和租约释放不能直接 `await`，要交给新任务；
   `chat.py:186` 的 `_release_or_background` 和取消路径的
   `_finalize_and_persist_after_cancel` 就是干这个的。
2. 不这样做时，DB 里的子代理块会停在 `running`，前端只能靠「非流式时把 running 显示成 stopped」
   兜底；修好之后，DB 会稳定落 `stopped`。

停止时 SSE 直接关闭、没有 `done` 终帧，这是**客户端协议边界**，不是 GA 问题：
GA 进程已经由上面的路径回收，实测 1 秒内归零，后续发送正常。

---

## 4. 一次完整往返，按图走一遍

对应时序图 [`ga-acp-sequence.html`](./ga-acp-sequence.html)：

1. 浏览器 `POST /api/sessions/:id/messages`，Hlaudz 建本轮 `TurnRuntime`。
2. 因为预检通过，主 Agent 的工具表里多了 5 个 `ga_*`。
3. 模型决定调用 `ga_spawn`，参数是一份**自包含任务书**。
4. `AcpSessionManager.spawn()` 拿配额、算 cwd、`Popen` 起 bridge、握手、建会话，
   然后后台 `session/prompt`，自己立刻返回 `agent_id`。
5. GA bridge 把 prompt 塞给 `GeneraticAgent`；GA 在自己的进程里跑回合、调自己的模型、
   用自己的 `file_*` / `code_run` 工具。
6. GA 每产出一段文本或一个工具事件，bridge 就发一条 `session/update`。
7. Hlaudz 的 `GaSession` 把它翻成 `subagent_event`，写进 `EventBuffer`；
   `_merged_events` 再把它并进 SSE，前端卡片实时刷新。
8. 主 Agent 调 `ga_wait`，等到 `completed`；再调 `ga_result(close=true)` 取最终答复并释放名额。
9. 主 Agent 把 GA 的结果写进自己的回答，SSE 发 `done`。
10. 本轮结束 `_cleanup_turn` / `close_all` 回收所有 GA 会话；GA 进程数归零。

如果中途停止，走的是 §3.10 的分支：`session/cancel` → 2 秒宽限 → 杀树。

---

## 5. 最小可跑示例

### 5.1 在 Hlaudz 里启用

假设 GA 装在 `C:/anaconda3/python.exe`，Hlaudz 后端这样启动：

```bash
cd D:/git_codes/inner_chatgpt/backend
INNER_ACP_GA_PYTHON=C:/anaconda3/python.exe python run.py
```

浏览器打开 `http://127.0.0.1:8000`，登录后在「设置 -> 我的模型」配一个模型（主 Agent 用）。
然后新建会话，发：

```text
请调用 ga_spawn 让 GA 计算 123*456，再用 ga_wait 等它完成，最后告诉我 GA 的结果。不要自己算。
```

预期看到：

- `ga_spawn` 工具步骤；
- 一张「GA 外援」卡片：`GenericAgent · 计算 123*456`；
- `ga_wait` / `ga_result` 两个步骤；
- 最终回答包含 `56088`。

这是一次真实跑通的例子：整轮约 11 秒，GA 用 `code_run` 执行 `print(123*456)`，
退出码 0，stdout 为 `56088`。

### 5.2 直接和 bridge 对话（不经过 Hlaudz）

如果你想确认「ACP 只是一条 stdio 协议」，可以绕过 Hlaudz，直接起 bridge：

```python
import json, subprocess, sys

proc = subprocess.Popen(
    [sys.executable, "-m", "frontends.acp.genericagent_acp_bridge"],
    cwd="D:/path/to/GenericAgent",
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, encoding="utf-8", bufsize=1,
)

def send(msg):
    proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
    proc.stdin.flush()

send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": 1, "clientCapabilities": {}}})
print(proc.stdout.readline())  # initialize result

send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
      "params": {"cwd": "D:/path/to/workspace", "mcpServers": []}})
print(proc.stdout.readline())  # sessionId

send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
      "params": {"sessionId": "<上一步的 sessionId>",
                 "prompt": [{"type": "text", "text": "计算 2+3，只回答数字"}]}})
while True:
    line = proc.stdout.readline()
    if not line:
        break
    msg = json.loads(line)
    if msg.get("id") == 3:
        print("done:", msg)
        break
    if msg.get("method") == "session/update":
        print("update:", msg["params"]["update"])
```

> 注意：`stderr=subprocess.DEVNULL` 只是演示写法。真实客户端必须**持续抽干 stderr**，
> 否则 bridge 的日志把管道写满，GA 会卡住。

---

## 6. 设计取舍：为什么不是别的做法

| 选择 | 为什么不选另一条路 |
| --- | --- |
| 做成内置工具，而不是 Skill | Skill 在本项目里只是提示词，不能起进程、持连接、回收子进程；这是能力，不是知识 |
| GA 做 ACP Server，Hlaudz 做 Client | GA 已经有完整的回合循环和工具；Hlaudz 只需要「派活 + 收结果」，不需要重写一个 Agent |
| 一会话一进程 | 隔离 stdout、工作目录、崩溃域和模型配置；代价是跨进程只能走协议 |
| 只有 root agent 拿到 `ga_*` | 防止递归派活导致数量和成本爆炸；子代理/teammate 的 registry 里根本没有这些工具 |
| `cwd` 由服务端算 | 不接受模型传入的路径；固定到 `user_workspace(user_id)`，避免越权读写别的用户目录 |
| 环境变量白名单 | 绝不把 Hlaudz 的密钥、DB 路径、`INNER_*` 传给 GA；GA 用自己的 `llm.yaml` |
| 两套模型配置 | GA 是独立产品，有自己的 profile/计费；Hlaudz 不应替它选模型或读它的密钥 |
| 不跨轮恢复 GA 会话 | `loadSession=false`；GA 会话按 generation 生命周期回收，需要续跑时重新写任务书 |
| 自动应答权限 | 当前集成以 `full_access` 为主；`ask` 模式下也可通过 `INNER_ACP_PERMISSION` 固定策略 |

### 6.1 安全边界

- **文件边界**：GA 的 `workspace_root` = 用户 workspace；Hlaudz 自己的文件工具也用同一个目录。
- **进程边界**：每个 GA 会话独立进程；`ProcessLease` 让 `/stop` 能精确杀到它。
- **环境边界**：只传系统必需变量 + 代理 + 显式白名单；不传主模型密钥。
- **模型边界**：GA 的模型配置在 `%APPDATA%/GenericAgent/llm.yaml`，Hlaudz 不读取、不落盘。
- **工具边界**：`ga_*` 只给 root agent；子代理/teammate 看不到。

---

## 7. 常见坑（都是实际踩过的）

1. **import bridge 会导致预检失败。** bridge 在 import 时把 stdout 重定向到 stderr；
   预检必须用 `find_spec` 只解析路径，不能执行模块。
2. **stdout 必须是纯净的 JSON 通道。** 任何 `print()`、子进程输出、Windows 文本模式换行
   都会破坏协议；所以有 fd 重定向、`set_inheritable(False)`、`O_BINARY`、`sys.stdout` router。
3. **stderr 必须持续抽干。** 管道写满后 GA 会阻塞；Hlaudz 用读线程 + 环形缓冲，
   出错时还能把末 N 行附到错误文本里。
4. **Windows 的 SelectorEventLoop 不支持 asyncio 子进程。** 用 `Popen` + 读线程 +
   `call_soon_threadsafe`，不要硬套 `create_subprocess_exec`。
5. **POSIX 上必须 `start_new_session=True`。** 否则 `killpg` 会把 Hlaudz 服务端自己杀掉。
6. **同一会话不能并发 prompt。** bridge 的 `prompt_lock` 和客户端的 `_active_prompt` 都会拒绝；
   并发派活不是排队，是直接报错。
7. **`ga_wait` 超时不杀进程。** 它只表示「还在跑」；要停必须 `ga_stop` 或本轮结束。
8. **`ga_result(close=true)` 不能把 `completed` 降级成 `stopped`。** 终态只允许进入一次。
9. **停止时 SSE 可能没有 `done` 终帧。** 这不是 GA 泄漏：`/stop` 先停 GA 再取消 SSE；
   即使客户端直接断开，`_cleanup_turn` 也会回收进程。第三方客户端要把「连接关闭」当停止处理。
10. **取消后不能再直接 `await` 写库/释放租约。** 已取消的任务里每个新 `await` 都会立刻抛
    `CancelledError`；终态持久化和租约释放必须交给新任务，否则 DB 会停在 `running`。

---

## 8. 代码索引

### GA 侧（Server）

| 主题 | 文件:行 |
| --- | --- |
| stdout / fd / Windows 二进制模式 | `frontends/acp/genericagent_acp_bridge.py:14` |
| `initialize` | `:202` |
| `session/new` | `:226` |
| 回调绑定 | `:264` |
| 权限请求 | `:273` |
| 工具事件 | `:330` |
| `session/prompt` | `:370` |
| 增量抽取 | `:425` |
| cancel / close / 分发 | `:469`、`:480` |
| stdio 主循环 | `:535` |

链接前缀：
`https://github.com/valkryhx/GenericAgent/blob/694820e/`

### Hlaudz 侧（Client）

| 主题 | 文件:行 |
| --- | --- |
| 配置与子进程环境 | `backend/app/acp/config.py:49`、`:174` |
| Popen / 管道 / 读线程 / 杀树 | `backend/app/acp/transport.py:170`、`:206`、`:123` |
| JSON-RPC 关联与三分发 | `backend/app/acp/client.py:52`、`:182`、`:198` |
| 会话状态机与事件映射 | `backend/app/acp/session.py:81`、`:134`、`:329`、`:465` |
| 多会话 / 配额 / 租约 | `backend/app/acp/manager.py:123`、`:162`、`:256`、`:450` |
| 5 个工具 | `backend/app/acp/tools.py:62` |
| root-only 挂载 | `backend/app/subagent/session.py:149` |
| SSE 合并 / 停止顺序 | `backend/app/routers/chat.py:47`、`:738` |
| 配额常量 | `backend/app/quota.py:36` |
| 用户 workspace | `backend/app/workspace.py:42` |
| 进程租约 | `backend/app/process_registry.py:13` |

链接前缀：
`https://github.com/valkryhx/inner_chatgpt/blob/de7e71c/`

---

## 9. 如果你想自己实现一个 Client

按这个顺序做，能少走很多弯路：

- 先写最小的 `initialize` / `session/new` / `session/prompt`，用一行一条 JSON 跑通；
- 再补请求关联表（`id -> Future`）和三类分发；
- 再补 `session/update` 的文本增量，确认流式输出；
- 再补工具事件和权限请求（Agent → Client 的请求必须回，否则 GA 会等死）；
- 再补 stderr 抽干和进程回收（Windows `taskkill /T /F`，POSIX `killpg` + `start_new_session`）；
- 再补超时、取消、终态幂等和「停止后立刻再发」；
- 最后才做 UI 卡片和持久化。

自检清单：

- bridge 装在目标 Python 里，预检用的是 `find_spec`，不是 import；
- stdout 只出现 JSON，没有 `print()` 或子进程日志；
- stderr 有人读，不会写满管道；
- 同一会话并发 prompt 会被拒绝；
- `ga_wait` 超时后 GA 还在跑；
- `ga_stop` 后进程树真的没了；
- 停止后同一会话能立刻再发；
- GA 用的模型配置和主 Agent 完全分开。

---

## 10. 验证记录（这套实现实际跑过的）

- 后端全量：**246 passed**；前端：**4 passed**；`npm run build` 成功。
- 真浏览器 + 真后端 + 真 GA：
  - 会话 `co_acafcb6b2f83`，指令「让 GA 计算 123*456」，11 秒完成，结果 `56088`；
  - 页面出现「GA 外援」卡片，`ga_wait` / `ga_result` 步骤全部完成；
  - DB 子代理块 `agent_id=ga_ed022fdf#1`、`role=ga`、`status=completed`。
- 停止路径：
  - 用户点停止后 GA 进程数 1 秒内归零；
  - 客户端直接断开、不调 `/stop`，同样 1 秒内归零；
  - 停止后同一会话立刻再发成功。
- 测试里专门覆盖了：配置 / transport / client / session / manager / tools / stop / 取消安全持久化。

> 这套实现的原则是：**协议层尽量薄，边界尽量硬，错误尽量可读。**
> 只要守住 stdout、进程、cwd、环境、配额这五条边界，ACP 本身其实很小。
