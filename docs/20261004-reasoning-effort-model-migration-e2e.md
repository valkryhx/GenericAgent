# 思考等级模型迁移真实验证记录

日期：2026-10-04（Asia/Shanghai）

## 结论

- DeepSeek V4.1 Flash 的 `none` 已通过真实 API 验证：流式和非流式 Responses 请求都返回 `DEEPSEEK_NONE_OK`，没有被 API 拒绝。因此 `none` 已加入 DeepSeek 的模型能力列表，配置为 `[none, low, high, max]`。
- 当前 gpt-6-luna 端点接受 `none`、`xhigh`、`max`，但明确拒绝 `ultra`，HTTP 400 返回的支持集合为 `none/minimal/low/medium/high/xhigh/max`。因此 Luna 配置和 Ink 选择器不应显示 `ultra`。

## 真实 API 冒烟

测试使用本机被 `.gitignore` 忽略的 `llm.yaml`，没有记录 API key、Authorization、完整响应或请求体。

| 模型 | effort | 结果 |
| --- | --- | --- |
| gpt-6-luna | none | 成功 |
| gpt-6-luna | xhigh | 成功 |
| gpt-6-luna | max | 成功 |
| gpt-6-luna | ultra | HTTP 400；服务端明确列出支持值不含 ultra |
| deepseek-v4.1-flash | low | 成功 |
| deepseek-v4.1-flash | max | 成功 |
| deepseek-v4.1-flash | none（stream） | 成功，返回 `DEEPSEEK_NONE_OK` |
| deepseek-v4.1-flash | none（非 stream） | 成功，返回 `DEEPSEEK_NONE_OK` |

## 自动化回归

- Python：`python -m unittest discover -s tests -v` → `1214 tests`，`OK`，`3 skipped`。
- Ink UI：`npm test` → `386/386` 通过。
- TypeScript：`npm run typecheck` → 通过。
- Python 语法：`python -m py_compile llm_config.py llmcore.py llm_client.py agentmain.py frontends/ink_bridge.py` → 通过。
- `git diff --check` → 通过（仅有 Git 的换行符提示）。

## 配置调整

- `gpt-6-luna` 和 `gpt-6-luna-chat` 从 `[none, minimal, low, medium, high, xhigh, max, ultra]` 收敛为 `[none, minimal, low, medium, high, xhigh, max]`。
- `deepseek-v4.1-flash` 保持独立的 `[none, low, high, max]`，没有把 Luna 的能力列表继承过去。
- wire 层仍然保留未知/未来等级的无损透传能力；这不代表某个模型的 UI 配置可以宣传未被该模型确认的等级。
