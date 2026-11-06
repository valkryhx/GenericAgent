#!/usr/bin/env python3
"""最小 ACP 客户端 demo：让「另一个程序 / agent」驱动已安装的 GenericAgent。

它真实启动 `python -m frontends.acp.genericagent_acp_bridge`，走 ACP JSON-RPC：
initialize -> session/new -> session/set_mode(ask) -> session/prompt
-> session/request_permission -> tool_call/update -> stopReason -> session/close。

用法（在任意目录）：
    python D:/git_codes/GenericAgent/scripts/acp_client_demo.py
    python .../acp_client_demo.py --deny          # 拒绝写文件，观察拒绝链路
    python .../acp_client_demo.py --keep-workspace
    python .../acp_client_demo.py --allow-source  # 允许从源码 checkout 跑桥

注意：demo 不会删除自己的临时工作目录，结束时打印路径，便于检查产物。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def compact(obj: Any, limit: int = 600) -> str:
    text = json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


class AcpClient:
    """stdio 上的 ACP 客户端；同时处理服务端发来的权限请求。"""

    def __init__(self, command: List[str], cwd: str, *, permission: str,
                 debug: bool = False) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.debug = debug
        self.permission = permission
        self.proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self._inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stderr_lines: List[str] = []
        self._next_id = 1
        self.updates: List[Dict[str, Any]] = []
        self.permissions: List[Dict[str, Any]] = []
        self.answer_text = ""
        self.tool_calls: Dict[str, Dict[str, Any]] = {}
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except json.JSONDecodeError:
                if self.debug:
                    print(f"[bad-json] {line[:300]}")

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_lines.append(line.rstrip())
            if len(self._stderr_lines) > 400:
                del self._stderr_lines[:200]
            if self.debug:
                print(f"[bridge] {line.rstrip()[:500]}")

    def _send(self, message: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _respond(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        if method != "session/request_permission":
            self._send({
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32601, "message": f"client does not support {method}"},
            })
            return
        self.permissions.append(msg)
        params = msg.get("params") or {}
        tool = (params.get("toolCall") or {}).get("title", "?")
        option_id = "allow_once" if self.permission == "allow" else "reject_once"
        print(f"[permission] 服务端请求审批: {tool} -> 客户端选择 {option_id}")
        self._send({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
        })

    def _handle_server_message(self, msg: Dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:
            self._respond(msg)
            return
        method = msg.get("method")
        if method == "session/update":
            update = (msg.get("params") or {}).get("update") or {}
            self.updates.append(update)
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                text = ((update.get("content") or {}).get("text") or "")
                self.answer_text += text
            elif kind == "tool_call":
                self.tool_calls[update.get("toolCallId", "")] = update
                print(f"[tool] {update.get('title')} kind={update.get('kind')} status={update.get('status')}")
            elif kind == "tool_call_update":
                self.tool_calls.setdefault(update.get("toolCallId", ""), {}).update(update)
                print(f"[tool] {update.get('toolCallId')} -> {update.get('status')}")
            elif self.debug:
                print(f"[update] {compact(update)}")

    def request(self, method: str, params: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"{method} timed out after {timeout}s")
            try:
                msg = self._inbox.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            if msg.get("id") == req_id and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {compact(msg['error'])}")
                return msg.get("result") or {}
            self._handle_server_message(msg)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def probe_bridge_module(workspace: Path) -> str:
    # 不要 import 这个模块做探测：它在导入期会把本进程 stdout 重定向到 stderr。
    code = (
        "import importlib.util; "
        "s = importlib.util.find_spec('frontends.acp.genericagent_acp_bridge'); "
        "print(s.origin if s else '')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(workspace),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    path = (proc.stdout or "").strip()
    if proc.returncode != 0 or not path:
        raise RuntimeError(f"无法导入 ACP bridge: {(proc.stderr or '')[:800]}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="GenericAgent ACP 客户端 demo")
    parser.add_argument("--workspace", help="demo 工作目录；默认新建临时目录")
    parser.add_argument("--permission", choices=("allow", "deny"), default="allow")
    parser.add_argument("--deny", action="store_true", help="等价于 --permission deny")
    parser.add_argument("--timeout", type=float, default=300.0, help="session/prompt 超时秒数")
    parser.add_argument("--allow-source", action="store_true", help="允许 bridge 来自源码目录")
    parser.add_argument("--debug", action="store_true", help="打印 bridge 日志与全部更新")
    parser.add_argument("--keep-workspace", action="store_true", help="保留工作目录（默认也保留）")
    args = parser.parse_args()
    permission = "deny" if args.deny else args.permission

    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path(
        tempfile.mkdtemp(prefix="ga_acp_demo_")
    )
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "acp_demo.txt"
    marker = "ACP_OK_030"

    print("=" * 68)
    print("GenericAgent ACP demo")
    print(f"workspace : {workspace}")
    print(f"permission: {permission}")
    print("=" * 68)

    module_path = probe_bridge_module(workspace)
    print(f"[install] bridge module: {module_path}")
    if not args.allow_source and "site-packages" not in module_path and "dist-packages" not in module_path:
        print("[FATAL] 当前跑的是源码版 bridge；加 --allow-source 才允许。", file=sys.stderr)
        return 2

    client = AcpClient(
        [sys.executable, "-m", "frontends.acp.genericagent_acp_bridge"],
        str(workspace),
        permission=permission,
        debug=args.debug,
    )
    exit_code = 0
    try:
        init = client.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "acp-client-demo", "version": "1.0"},
        })
        print(f"[init] protocol={init.get('protocolVersion')} agent={compact(init.get('agentInfo'))}")

        session = client.request("session/new", {"cwd": str(workspace), "mcpServers": []})
        session_id = session.get("sessionId")
        print(f"[session] id={session_id}")
        print(f"[session] modes={compact(session.get('modes'))}")

        mode = client.request("session/set_mode", {"sessionId": session_id, "modeId": "ask"})
        print(f"[mode] applied={mode.get('modeId')}")

        prompt = (
            "请使用 file_write 工具在当前工作目录创建文件 acp_demo.txt。"
            f"文件内容严格为这一行：{marker}\n"
            "不要询问用户，不要写别的内容。完成后只回复 DONE。"
        )
        print("[prompt] 发送任务：用 file_write 创建 acp_demo.txt")
        result = client.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
            timeout=args.timeout,
        )
        stop_reason = result.get("stopReason")
        print(f"[done] stopReason={stop_reason}")
        print(f"[stream] assistant_chunks={len([u for u in client.updates if u.get('sessionUpdate') == 'agent_message_chunk'])}")
        print(f"[stream] tool_calls={len(client.tool_calls)} permissions={len(client.permissions)}")
        print("[assistant] " + client.answer_text.strip().replace("\n", " ")[:500])

        exists = target.is_file()
        content = target.read_text(encoding="utf-8", errors="replace") if exists else ""
        matched = marker in content
        print(f"[verify] file_exists={exists} marker_present={matched}")
        if exists:
            print(f"[verify] content={content.strip()[:200]!r}")

        if permission == "allow":
            ok = bool(session_id) and stop_reason == "end_turn" and exists and matched
        else:
            ok = bool(session_id) and stop_reason == "end_turn" and not matched

        summary = {
            "ok": ok,
            "workspace": str(workspace),
            "bridge_module": module_path,
            "session_id": session_id,
            "stop_reason": stop_reason,
            "permission_mode": permission,
            "permission_requests": len(client.permissions),
            "tool_calls": len(client.tool_calls),
            "file_exists": exists,
            "marker_present": matched,
            "assistant_text": client.answer_text,
        }
        (workspace / "demo_result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        print(f"[result] ok={ok} -> {workspace / 'demo_result.json'}")

        client.request("session/close", {"sessionId": session_id}, timeout=30)
        exit_code = 0 if ok else 1
    except Exception as exc:
        print(f"[FATAL] {type(exc).__name__}: {exc}", file=sys.stderr)
        if client._stderr_lines:
            print("[bridge stderr tail]", file=sys.stderr)
            for line in client._stderr_lines[-20:]:
                print("  " + line[:500], file=sys.stderr)
        exit_code = 3
    finally:
        client.close()
        print(f"[workspace] 保留在 {workspace}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
