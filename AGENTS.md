# Repository Guidelines

## 项目结构与模块组织

GenericAgent 是一个紧凑的 Python 项目。核心运行时代码位于仓库根目录，包括 `agentmain.py`、`agent_loop.py`、`ga.py`、`llmcore.py` 和 `simphtml.py`。可安装的 CLI 包在 `ga_cli/`，`ga` 命令入口映射到 `ga_cli.cli:main`。各类界面和聊天/机器人适配器位于 `frontends/`；图片、皮肤和静态资源位于 `frontends/skins/` 与 `assets/`。长期记忆、SOP 和辅助工具位于 `memory/`，反射与自主运行辅助逻辑位于 `reflect/`，可选集成放在 `plugins/`。测试统一放在 `tests/`。

## 构建、测试与本地运行

- `python -m pip install -e .`：以 editable 模式安装核心包和 `ga` 命令。
- `python -m pip install -e ".[ui]"`：安装核心依赖和桌面/TUI UI 依赖。
- `python launch.pyw`：启动默认桌面界面。
- `python frontends/tuiapp.py`：启动终端 UI。
- `streamlit run frontends/stapp2.py`：启动 Streamlit 前端。
- `python -m unittest discover -s tests`：运行当前测试套件。

只安装正在修改的前端或机器人适配器所需的可选依赖。

## 编码风格与命名规范

使用 Python 3.10-3.13。代码应保持紧凑、可读，并贴合现有文件风格。优先使用自解释的函数和变量，少写解释性注释。避免过宽的 `try/except`，重要错误应清晰暴露。模块、函数和变量使用 `snake_case`，类名使用 `PascalCase`。新增模块应靠近功能边界，例如 UI 适配放在 `frontends/`，可选集成放在 `plugins/`。

## 测试指南

测试使用标准库 `unittest`。测试文件命名为 `test_*.py`，放在 `tests/`。新增前端或适配器行为时，应 stub 外部服务，避免依赖真实 API 凭据。提交前运行 `python -m unittest discover -s tests`，修复 bug 时补充聚焦的回归测试。

## 提交与 PR 规范

近期历史使用 Conventional Commits，例如 `feat(tui): ...`、`fix(tgapp): ...`、`docs: ...` 和 `refactor: ...`。提交应小而聚焦。PR 应说明背景、概述行为变化、列出验证命令；只有可见 UI 变化才附截图。避免不必要的新依赖和大范围重构。

## 安全与配置提示

不要提交真实 API key 或本地密钥。配置示例应维护在 `mykey_template.py`、`mykey_template_en.py` 或 `assets/configure_mykey.py`。本地生成状态、日志和凭据应保持在版本控制之外。
