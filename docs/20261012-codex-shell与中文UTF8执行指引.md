# Codex 在 Windows 下的 Shell 与中文 UTF-8 执行指引（优化版）

- 日期：2026-10-12
- 目的：修正「Codex 经常误用 PowerShell / 中文写文件乱码」的反复问题，给出一份可直接替换/补充 developer_instruction 的说明。
- 适用环境：Windows 10/11 + Git for Windows（Git Bash）+ 本仓库 `D:\git_codes\GenericAgent`。

---

## 0. 一句话结论

**本环境 `exec_command` 的默认 shell 是 PowerShell。**
要写文件、跑 UTF-8 中文命令，**必须显式传 `shell="bash"` 且 `login=false`**；不显式指定时走的是 PowerShell，中文与 `<<`/`$(...)`/`&&` 等 Bash 语法都会出问题。

---

## 1. 根因：默认 shell 是 PowerShell，不是 Git Bash

实测（同一环境、同一工作目录）：

| 调用 | 输出 | 结论 |
| --- | --- | --- |
| `shell="bash"`, `login=false` | `0=/usr/bin/bash`、`BASH_VERSION=5.2.37`、`MINGW64_NT-10.0-26200` | 走 **Git Bash** ✅ |
| 省略 `shell` | `0=` 空、`BASH_VERSION=` 空、`PSVersion=...` 存在 | 走 **PowerShell** ❌ |

**这解释了此前的矛盾现象**：agent 口头说「我在 Git Bash」，但实际执行的是 PowerShell，于是出现：

- `ParserError: 重定向运算符后缺少文件规范`（PowerShell 不认 `<<`、`<`、`>` 的 bash 用法）；
- 中文文件名/参数经 shell 往返后变成乱码（`\udc89` 之类的 surrogate）；
- `&&`、`$(...)`、单引号 here-string 行为与预期不符。

> 结论：**不要依赖默认 shell，也不要「声称」自己在 Git Bash——用 `shell` 参数显式声明。**

---

## 2. 正确用法（推荐模板）

```text
exec_command(
  cmd   = "<bash 脚本>",
  shell = "bash",        # ← 必须显式；否则走 PowerShell
  login = false,         # ← 默认；除非确实需要 .bashrc 里的别名/环境
  workdir = "D:\git_codes\GenericAgent",
)
```

### 2.1 处理中文路径 / 参数 / 输出时，额外设置 locale

```bash
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
```

放在脚本最前面。这样 Git Bash 里的 `ls`、`grep`、`python`、`git` 对 UTF-8 中文的处理才一致，避免被系统 GBK(936) 干扰。

### 2.2 写文本文件：一律 UTF-8 无 BOM、LF

- Bash 里用 **单引号 heredoc**，且整个 heredoc 交给 `cat > file`（不要经 `python -c` 的文本参数往返）：

  ```bash
  cat > "docs/中文名.md" << 'EOF'
  ...中文内容...
  EOF
  ```

- Python 写文件必须显式：

  ```python
  open(path, "w", encoding="utf-8", newline="\n")   # 不要用默认 newline，否则 \n 会被翻成 \r\n
  ```

- **禁止**用 GBK / 936 / ANSI 作为「绕过乱码」的手段——那只会把乱码写进文件。

---

## 3. 已验证会踩的坑（含复现与规避）

| # | 现象 | 触发 | 规避 |
| --- | --- | --- | --- |
| 1 | `ParserError: 重定向运算符后缺少文件规范` | 省略 `shell`，用 `<<`/`<`/`>` | 显式 `shell="bash"` |
| 2 | 中文文件名变 `ʵ��ȱ���` 之类 | 中文经 shell 参数往返 | 文件名尽量 ASCII，或用 heredoc 直写 |
| 3 | `UnicodeEncodeError: ... '\udc89'` | `python -c` 文本参数 + 中文，Python 按控制台编码解码 | 中文内容走 `cat` heredoc 或 `sys.stdin.buffer` |
| 4 | 控制台 `print` 中文显示乱码，但文件其实是好的 | 控制台代码页 GBK | **以字节校验为准**（见 §5），不要相信终端回显 |
| 5 | `rm -f` / 递归删除被拒 | 安全规则 | 用 Python `os.remove()` 删单个文件；不递归删 |
| 6 | `python3` 命中 WindowsApps 存根 | PATH | 用 `python` |

---

## 4. 规则清单（可直接并入 developer_instruction）

1. **命令执行统一用 Git Bash**：`exec_command` 必须显式 `shell="bash"`、`login=false`；只有需要 shell 启动文件时才 `login=true`。
2. **不要切 shell 绕过沙箱失败**：若 Git Bash 命令因 Windows 沙箱失败，重试时用 `sandbox_permissions="require_escalated"` + 窄范围 justification，而不是换 PowerShell/cmd。
3. **中文/UTF-8 场景显式设 locale**：`LANG=C.UTF-8`、`LC_ALL=C.UTF-8`。
4. **文本文件 UTF-8 无 BOM、LF**：Python 用 `encoding="utf-8", newline="\n"`；禁用 GBK/936/ANSI。
5. **中文内容优先 heredoc 直写**，避免经 `python -c` 的参数或 shell 变量往返。
6. **中文校验看字节，不看终端回显**：用 `python` 读 `rb` 后 `decode("utf-8")` 或 `hex` 判断。
7. **删除**：绝不 `rm -rf`/递归删除；单文件用 Python `os.remove()`，且不删工作目录之外。
8. **escalation 只读/窄范围**，不用于绕过安全规则。

---

## 5. 中文正确性校验方法（推荐固化）

写完含中文的文件后，用**字节级**校验，而不是肉眼/终端：

```python
import os
raw = open(path, "rb").read()
txt = raw.decode("utf-8")        # 不抛异常 => 是合法 UTF-8
assert "\r" not in txt           # LF-only
print(txt.split("\n")[0])        # 需要看中文时，用 sys.stdout.buffer 写 UTF-8
```

输出中文到控制台时，用：

```python
import sys
sys.stdout.buffer.write(("校验行：" + txt.split("\n")[0] + "\n").encode("utf-8"))
```

这样即使控制台是 GBK，也能确认磁盘内容正确。

---

## 6. 为什么「显式 shell」比「改全局默认」更可靠

- `exec_command` 的默认值是 PowerShell（本环境实测），且不同会话/工具可能不一致；
- 依赖默认值 = 让 agent 每次都赌一次；
- **每次调用显式写 `shell="bash"`** 是最小、最确定、可审计的做法；
- 若一定要设默认，应设在工具/运行时层，而不是靠 agent 记忆。

---

## 7. 附：本次诊断证据

```text
# shell="bash"
SHELL=/usr/bin/bash.exe
0=/usr/bin/bash
BASH_VERSION=5.2.37(1)-release
MINGW64_NT-10.0-26200 valkry 3.5.4-395fda67.x86_64 2024-11-25 09:49 UTC x86_64 Msys

# 省略 shell
0=
BASH_VERSION=
PSVersion=System.Management.Automation.PSVersionHashTable
```

> 说明：本机同时存在 Git Bash 与 PowerShell；`exec_command` 不显式指定 `shell` 时走后者。
