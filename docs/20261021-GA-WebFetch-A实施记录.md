# 20261021 GA WebFetch A 路线实施记录

> 范围：本次只实施 A 路线（透明网络出口优先）。B 路线（全量 DNS/私网分类、
> DNS rebinding、peer 复核、代理侧信任模型）没有实施，仍作为后续方案保留。

## 1. 结论

- `web_fetch` 已接入 GenericAgent 的工具分发、权限体系、schema、hooks 生命周期和审计边界。
- 网络出口固定走进程级透明网络栈：`requests.Session.trust_env = True`。
- 工具参数中没有 `proxy`、`direct`、`vpn` 或 `egress` 开关。
- 返回值固定标记：
  ```json
  {"egress":{"mode":"transparent","security_assurance":"basic"}}
  ```
- `basic` 表示没有完成完整 SSRF 验证，尤其不能声称已覆盖 DNS rebinding、
  代理 CONNECT 后的目标 peer 复核或全量私网地址分类。
- 本机真实网络 E2E 已通过：`https://example.com/` 在当前网络环境成功抓取。

## 2. 实际落盘模块

实施目录：`genericagent/tools/web_fetch/`

| 文件 | 职责 |
| --- | --- |
| `models.py` | `WebFetchRequest`、稳定错误码对象 `FetchError` |
| `policy.py` | HTTPS-only、userinfo/控制字符/dotless/IP literal/metadata host 等 URL 基础策略 |
| `egress.py` | 透明代理事实采集、SOCKS 明确拒绝、`requests.Session` 构建 |
| `metadata.py` | 单次解析的云 metadata 窄地址拦截 |
| `transport.py` | `FetchLimits`、流式读取、解压限制、手动逐跳重定向 |
| `extract.py` | 编码、HTML→Markdown、JSON 格式化、二进制隔离 |
| `tool.py` | 工具编排、结果契约、审计与缓存接入 |
| `audit.py` | 凭据/正文安全的 JSONL 审计 |
| `cache.py` | 有界成功结果缓存 |
| `__init__.py` | 最小公共导出 |

GA 接入变更：

- `genericagent/core/handler.py`：新增 `do_web_fetch`，复用现有 `dispatch()` 的
  capability、permission、PathACL、PreToolUse 和 PostToolUse 流程。
- `genericagent/core/permissions/policy.py`：加入只读静态工具集合。
- `genericagent/workflow/permissions.py`：加入 workflow 只读静态工具集合。
- `genericagent/workflow/path_acl.py`：加入非文件系统工具集合。
- `assets/tools_schema.json`、`assets/tools_schema_cn.json`：各增加一个 `web_fetch` 条目。

## 3. 透明出口与依赖

### 3.1 网络行为

- 显式设置 `trust_env=True`；
- 自动继承 `HTTPS_PROXY` / `https_proxy`、`HTTP_PROXY` / `http_proxy` 和 `NO_PROXY`；
- Windows 系统代理由 `requests` 的 urllib 平台代理探测路径处理；
- TUN/VPN 位于更底层路由，工具不读取也不切换网络模式；
- TLS 校验固定为 `verify=True`；
- 检测到 SOCKS 代理变量时返回 `EGRESS_UNAVAILABLE`，不静默降级直连。

当前环境：Python 3.12.4、requests 2.31.0、urllib3 2.6.3、beautifulsoup4 4.12.3。
`requests` 与 `beautifulsoup4` 已在 GA 的 `pyproject.toml` 依赖和本机环境中存在，
A 没有新增第三方依赖。

### 3.2 重要边界

透明代理/TUN 的成功证明“网络可用”，不证明“目标 IP 已被本地验证”。
当流量经 HTTP CONNECT 或 TUN 转发时，本地进程不能仅靠 `requests` 的公开 API
可靠取得 CONNECT 后的目标 peer；因此 A 只能记录 `security_assurance=basic`。

## 4. URL、重定向与资源限制

### 4.1 默认 URL 策略

- 默认只允许 HTTPS；HTTP 仅保留给测试或明确启用 `allow_http` 的内部调用；
- URL 最大 2000 字符；
- 拒绝 userinfo、控制字符、相对 URL、authority 反斜杠混淆；
- 拒绝 IP literal、`localhost`、dotless host、常见 metadata hostname；
- 目标端口只允许 80、443、8080、8443；
- fragment 被移除，query 保留用于请求，但审计时脱敏。

### 4.2 重定向

- `allow_redirects=False`；
- 手动处理 301、302、303、307、308；
- 最多 5 跳；
- 同站自动跟随，跨站返回 `REDIRECT_CROSS_SITE`；
- 每一跳重新执行 URL/host 校验；
- 每一跳重新执行 metadata 窄地址检查；
- 重定向响应同样受 header、时间和跳数限制。

### 4.3 限制值

| 限制 | 默认值 |
| --- | --- |
| header | 16 KiB |
| 压缩前 body | 2 MiB |
| 解压后 body | 10 MiB |
| 总 deadline | 15 秒 |
| connect timeout | 3 秒 |
| read timeout | 5 秒 |
| 流式 chunk | 64 KiB |

`Content-Length` 只做早期拒绝；实际读取时始终累加计数，超限立即终止。
gzip/deflate 解压后再次检查硬上限。

## 5. 内容、审计与缓存

### 5.1 内容处理

- HTML/XHTML 转 Markdown；
- `text/markdown`、`text/plain` 直接返回；
- JSON 解析后格式化；
- XML 与 JavaScript 按文本返回；
- 解码失败返回 `DECODE_FAILED`，不静默换编码；
- 二进制类型不进入模型上下文；
- 正文统一包裹在 `<fetched-web-content>` 中，并附加不可信数据处理提示。

### 5.2 审计

设置 `GA_WEBFETCH_AUDIT_PATH` 后写 JSONL。记录包含：

- URL 与 `final_url`（query 统一脱敏为 `?redacted=1`）；
- 重定向链；
- `egress_mode=transparent`；
- `security_assurance=basic`；
- 状态、HTTP 状态、字节数、耗时、错误码。

不记录网页正文、Cookie、Authorization 或代理凭据。

### 5.3 缓存

设置 `GA_WEBFETCH_CACHE_DIR` 后启用。只缓存成功结果，默认 TTL 300 秒、
最多 128 条；缓存 key 包含 URL、offset、egress mode、security assurance 和
policy version。当前实现没有后台过期清理任务，过期条目在读取时失效，
并会在后续成功写入后按数量裁剪。

## 6. 验证结果

### 6.1 聚焦回归

```text
python -m unittest tests.test_web_fetch_models \
  tests.test_web_fetch_url_policy tests.test_web_fetch_egress \
  tests.test_web_fetch_metadata tests.test_web_fetch_transport \
  tests.test_web_fetch_extract tests.test_web_fetch_tool \
  tests.test_web_fetch_audit tests.test_web_fetch_cache \
  tests.test_web_fetch_permissions tests.test_web_fetch_schema \
  tests.test_workflow_path_acl -v
```

结果：`Ran 77 tests ... OK`。

### 6.2 真实网络 E2E

```text
GA_RUN_NETWORK_E2E=1 python -m unittest tests.test_web_fetch_live_egress -v
```

结果：`Ran 1 test ... OK`。当前机器上 `https://example.com/` 成功返回，
结果带有 `egress.mode=transparent` 与 `security_assurance=basic`。

### 6.3 全量回归

```text
python -m unittest discover -s tests
```

结果：`Ran 1716 tests in 224.721s`，`OK (skipped=4)`。

```text
python -m compileall -q genericagent
git diff --check
```

结果：两者均通过。

### 6.4 验证中修复的问题

- 发现并修正了 schema 编辑误把 `additionalProperties: false` 写入 `code_run` 的问题；
- 在 `tests/test_web_fetch_schema.py` 增加防回归断言，确保 `code_run` schema 不被误改。

## 7. 与方案文档的差异

| 方案项 | 实际实现 | 状态 |
| --- | --- | --- |
| `limits.py` 单独模块 | `FetchLimits` 定义在 `transport.py` | 结构简化，不改变契约 |
| 二进制保存会话附件 | 仅说明“未进入模型上下文” | 未实现附件保存 |
| `prompt` 驱动模型提炼 | 参数被接收，但 P0 不调用模型 | 未实现，留到 P1 |
| 重定向逐跳 DNS/IP 全量复验 | 只做 URL 校验与 metadata 窄检查 | 未实现，属于 B |
| DNS rebinding / peer 复核 | 不实现 | 明确属于 B |
| 代理 CONNECT 后目标 IP 验证 | 无法由当前 requests 公开接口可靠完成 | 明确属于 B |
| 代理模式安全判定 | 只声明 transparent + basic | 不夸大保证 |

## 8. 验收判断

A 路线可判定为“透明网络出口和基础抓取已完成”，但不判定为“完整 SSRF 防护完成”。

后续讨论 B 时，应先决定代理/TUN 场景的信任边界：是信任代理侧策略，还是引入
可验证目标的隧道/代理协议。这个决定会直接影响 DNS rebinding、peer 复核和
私网地址全量分类能否做成强保证。


## 9. 正文质量、浏览器 UA 与空内容补强（2026-10-21）

### 9.1 结论与动机

这轮补强不冒充 CC 既有实现：

- CC 303 没有统一 `EMPTY_CONTENT`；
- CC 303 的目标站点 UA 是 `Claude-User (claude-code/2.1.303; ...)`，不是浏览器 UA；
- CC 303 只用 Turndown 并移除 `script/style/noscript/iframe`，没有正文候选评分、
  link-density 或 GitHub/百度站点特判；噪声主要靠 side model 的 prompt 提炼。

GA 因此把这三项作为 A 路线的内容质量补强，不改变“透明出口 + basic 安全保证”的边界。

### 9.2 浏览器 UA

- 默认值改为 Chrome-like UA：
  `Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/128.0.0.0 Safari/537.36`；
- 可通过环境变量覆盖：
  `GA_WEBFETCH_USER_AGENT`；
- 拒绝包含 CR/LF/控制字符的配置值，并限制最大 512 字符；
- 目标页面默认不再发送 `GenericAgent-WebFetch/0.1`。

### 9.3 正文候选评分与导航剔除

HTML 处理现在分两步：

1. 清理 `script/style/noscript/iframe/template/svg/canvas`；
2. 清理 `nav/footer/aside`、导航 role、隐藏元素、全局 header，以及明显导航类名；
3. 对 `main/article/[role=main]` 和 `div/section/td/body` 候选评分：
   - 正文文本长度；
   - `p`、标题、`pre` 数量；
   - link text 占比；
   - 重复链接/许可证/版权块降权；
4. 选中候选后输出 Markdown，再做空内容与导航质量判定。

这不是 GitHub/百度域名特判。候选评分只是启发式，无法保证所有 SPA、反爬页面或
纯链接索引页都能得到“完美正文”；无法确认正文时仍返回 `EMPTY_CONTENT`，而不是
把导航塞进上下文并报成功。

### 9.4 空内容与 offset 契约

新增稳定错误：

```json
{
  "status": "error",
  "error": {
    "code": "EMPTY_CONTENT",
    "message": "Response contained no extractable body text (empty content)",
    "details": {}
  }
}
```

触发条件：

- HTML 清理后没有可读正文文本；
- 只剩导航/页脚/许可证/版权等明显非正文链接块；
- offset 越过正文末尾，切片为空；
- 227-byte JavaScript redirect shell 这类无正文页面。

正常短文本、代码块、Release Notes 链接列表不会被简单按“短”或“链接多”误杀。
`EMPTY_CONTENT` 不写缓存；审计记录仍保存脱敏 URL、`final_url`、HTTP 状态、
字节数、耗时和 `error_code`。

### 9.5 缓存版本

内容策略变化会改变同一 URL 的提取结果，因此：

```text
ga-web-fetch-a-v1 -> ga-web-fetch-a-v2
```

旧缓存不会继续返回旧的空正文/导航结果。

### 9.6 实测结果

在当前机器透明代理/TUN 环境下：

| URL | 结果 |
| --- | --- |
| `https://www.baidu.com/` | 旧实现为 227-byte 空壳 success；浏览器 UA 可获取完整页面，但正文提取后只剩导航/页脚，现返回 `EMPTY_CONTENT` |
| `https://github.com/openai/` | 旧实现约 332 KB，开头为整段 Navigation Menu；现约 6.6 KB，输出 OpenAI 标题、Pinned repositories 与仓库正文 |
| `https://bbs.nga.cn/?rand=267` | 仍稳定返回 `HTTP_STATUS` 403，说明没有为了“成功”而伪造内容 |

### 9.7 聚焦验证

```text
python -m unittest tests.test_web_fetch_extract \
  tests.test_web_fetch_tool tests.test_web_fetch_transport -v
```

结果：全部通过（提取 18、工具 8、传输 17）。

完整回归结果见下一节。

### 9.8 最终回归

```text
python -m unittest discover -s tests
```

结果：

```text
Ran 1734 tests in 212.838s

OK (skipped=4)
```

首次全量运行时 `tests.test_workflow_runtime.WorkflowCancellationSemanticsTest.\
test_runtime_observes_external_cancel_of_running_workflow` 曾一次报
`workflow runtime deadline exceeded`；该测试单跑 5/5 通过，第二次全量回归也通过。
这是既有 workflow 计时竞态，不在 WebFetch 改动路径，本轮未修改该模块。

```text
python -m compileall -q genericagent
git diff --check
```

两者均通过。

真实网络最终探针：

```text
https://example.com/                 success  304 result chars
https://www.baidu.com/               error    EMPTY_CONTENT
https://github.com/openai/           success  6589 result chars
https://bbs.nga.cn/?rand=267         error    HTTP_STATUS 403
```

结论：A 路线现有透明代理/TUN、HTTPS-only、逐跳校验和资源限制不变；本轮只补强
正文质量、UA 兼容性与 `EMPTY_CONTENT` 成功/失败语义，不把安全保证升级为完整 SSRF 防护。
