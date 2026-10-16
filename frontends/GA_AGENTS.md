# GA 前端层项目说明

本文件适用于 `frontends/` 下的桌面、TUI、Web、Ink、机器人和桥接适配代码。它补充根目录运行时说明，不重复根目录的 LLM/provider 或通用安全规则。

## 目录结构

- `frontends/common/`：跨前端共享的聊天命令、历史输入等 helper。
- `frontends/terminal/`：Textual TUI、终端命令和会话恢复。
- `frontends/ink/`：React/Ink UI 与 Python JSONL bridge。
- `frontends/desktop/`：Qt、Feishu、桌面宠物。
- `frontends/web/`：Streamlit 前端。
- `frontends/bots/`：Telegram、QQ、Discord、钉钉、微信、企业微信。
- `frontends/acp/`：ACP bridge。

顶层同名 `*.py` / `*.pyw` 是兼容 shim，不是实现文件；新代码必须写入上述子目录。

## 前端边界

- 前端适配层应尽量通过稳定的 GA runtime / bridge API 交互，不要绕过 `GenericAgent` 主循环直接改写后端历史。
- UI 问题要优先归约为可测试的协议事件、stdout 字节、布局几何或纯函数行为。
- 桥接协议应保持 stdout 机器可读；调试日志应写入日志文件或 stderr 重定向位置，不能污染 JSONL 协议流。

## 修改建议

- 修改 `frontends/ink/bridge.py`（旧 `frontends/ink_bridge.py` 仅作兼容 shim） 时，优先补 Python bridge 层单测，尤其是 session resume、permission、compact、workflow 和 JSONL 协议边界。
- 修改非 Ink 前端时，保持依赖按需安装，不要把所有可选 UI / bot 依赖提升为核心依赖。
- 前端新增配置项时，应考虑 CLI、bridge 和直接启动三种路径是否一致。
