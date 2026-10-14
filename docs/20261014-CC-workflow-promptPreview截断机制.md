# CC workflow 的 promptPreview / resultPreview —— 400 字头部截断机制（2026-10-14）

> 触发问题：`_workflow_run_payload` 里的 job `prompt`「截断」，
> 是对齐 CC 的什么？截多少？是后面的内容丢弃吗？
> CC 证据仓（只读）：`D:\git_codes\claudecode283_hack`。
> **云端不复刻：复刻时暂时不需要参考 claudecode 云端会话的实现。**

---

## 1. 一句话回答

CC 在 workflow 的**事件通道**里从不传 agent 的**完整 prompt**，只传一个**预览**字段
`promptPreview`（和结果预览 `resultPreview`）。预览的生成规则是：

```js
Co = 400
Mt(e):  r = (typeof e==="string" ? e : b(e)).trim()
        return r.length > Co ? re(r, Co) + "\u2026" : r
```

即：**超过 400 个字符就取头部 400 个字符，追加一个省略号 `…`，尾部丢弃**；
不足 400 则原样返回。**全文不丢**——它在 durable record 与 agent transcript 里，
由详情面板**按需加载**。

---

## 2. 源码证据（逐条坐标）

### 2.1 预览助手 `Mt` 与常量 `Co`

- 定义文件：`reconstructed/renamed/m0869.Mt.36742.js`（源：`m0869_0xd12567a.js @36742`）

```js
function Mt(e) {
  if (e == null) return;
  let r = (typeof e === "string" ? e : b(e)).trim();
  if (!r) return;
  return r.length > Co ? re(r, Co) + "\u2026" : r;
}
```

- 常量：同模块内 `Co=400`。
- `b(e)`：把非字符串值拍平成字符串（消息内容 → 文本）。
- `re(r, Co)`：头部截断助手（按码点安全切片），从 `chunk-y04k89s2.js` 导入
  （该 chunk 未单独抽取，故看不到实现；语义等价于「取前 Co 个字符」）。

### 2.2 用途：`workflow_agent` 事件只带预览

`m0869_0xd12567a.js` 里，每个子代理的进度/生命周期事件都带：

- `promptPreview: Mt(U)`（U = agent 的 prompt）
- `resultPreview: Mt(te.result)`（agent 结果）
- 另有 `promptPreview: He` / `promptPreview: In` 等（同一助手）。

也就是说，**跑 workflow 时流经事件通道的，是 400 字预览，不是全文**。

### 2.3 详情面板：优先按需全文，回退预览

`m1460_0xd973957.js`（workflow 详情面板）里：

```js
rt = a!=="loading" && a?.prompt ? a.prompt : o.promptPreview ?? ""
G  = rt ? Ir(rt, y).map(...)   // Ir = 按面板宽度换行
   : [[{ text: l==="queued" ? "Available once the agent starts."
              : a==="loading" ? "Loading\u2026"
              : l==="running" ? "Not available yet (agent still running)."
              : "Transcript not available." , dimColor:true }]]
```

含义：

- 若 agent 记录**已加载**（`a.prompt` 有值）→ 显示**完整 prompt**（按面板宽度换行，不截断）；
- 否则（尚未加载）→ 退回 `o.promptPreview`（即 400 字预览）；
- 连预览都没有 → 显示 `Loading…` / `Not available yet (agent still running).` 等占位。

这就是 CC 的「**预览在事件里，全文按需读 transcript**」设计。

---

## 3. GA 现状 vs CC

| 维度 | CC | GA（改前） |
| --- | --- | --- |
| 事件通道里的 prompt | `promptPreview` = 400 字 + `…` | `_workflow_run_payload` 带**完整** `job.prompt`（最大 ~440 KB/run） |
| 结果预览 | `resultPreview` = 400 字 | 进度快照有 `resultPreview`（240 字，`workflow_store._preview`） |
| 面板 prompt 来源 | 全文（按需 transcript）→ 回退 `promptPreview` | 首选 `job.prompt`（全文）→ 回退 `entry.promptPreview`（240）→ 回退 draft |
| 全文位置 | durable record + transcript | `state.json`（durable） + `agents/*.jsonl` transcript |

**问题**：GA 的 `_workflow_run_payload` 每次都把**完整 prompt** 塞进事件（最大 ~440 KB），
而面板只是拿它显示一行「Prompt」。这正是之前 detail/list 帧偏大的次要来源。

---

## 4. 本轮对齐（实施）

在 `frontends/ink_bridge.py::_workflow_run_payload` 里，对每个 job 的 `prompt` 做 CC 口径截断：

- **长度 = 400 字符**（对齐 `Co=400`）；
- 超出则**头部 400 + `…`（U+2026）**，**尾部丢弃**；
- **durable `state.json` 仍保留全文**（只在发往 UI 的 payload 边界截断），
  与 CC「全文留在 durable record / transcript，事件只发预览」一致；
- 面板**无需改动**：仍首选 `job.prompt`，只是它现在是 400 预览（等价 CC 的 `promptPreview`）。

---

## 5. 验证

- `_workflow_run_payload` 的 job `prompt` 长度 ≤ 401（400 + 省略号）。
- 既有测试（`test_ink_bridge` 等）不受影响（断言用的 prompt 都是短串）。
- durable `state.json` 的 `jobs[].prompt` 仍为全文（未被截断）。
