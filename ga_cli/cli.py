"""
ga_cli/cli.py - GenericAgent 命令行分发系统

通过 python -m ga_cli <命令> 或 ga <命令> 调用
"""
import os, sys, subprocess, argparse, textwrap, shutil
from pathlib import Path

# Windows GBK 终端兼容
if sys.platform == "win32" and sys.stdout.encoding and sys.stdout.encoding.lower() in ("gbk", "gb2312"):
    sys.stdout.reconfigure(errors="replace") if hasattr(sys.stdout, "reconfigure") else None

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _source_project_dir() -> str:
    candidate = PACKAGE_PARENT
    if (candidate / "pyproject.toml").is_file() or (candidate / "ga").is_file():
        return str(candidate)
    return ""


def _runtime_project_dir() -> str:
    override = os.environ.get("GA_PROJECT_ROOT")
    if override:
        return str(Path(override).expanduser().resolve())

    # The caller's project owns the workspace, including editable installs where
    # GA itself is imported from a source checkout. A user project may have its
    # own pyproject.toml; that must not redirect the session into GA's checkout.
    from genericagent import paths as ga_paths

    caller = Path.cwd().resolve()
    caller_project = ga_paths._project_boundary(caller)
    source = _source_project_dir()
    if source:
        source_path = Path(source).resolve()
        if caller == source_path or source_path in caller.parents:
            return str(source_path)
        if caller_project == caller and not (caller / ".git").exists():
            return str(caller)
        return str(caller_project)

    # Installed wheel: the project workspace is the caller's current directory,
    # not site-packages and not GA's private user-data directory.
    return str(caller_project)


PROJECT_DIR = _runtime_project_dir()



def project_environment(*, project=None, launch=None, data_root=None):
    """Return the project identity snapshot inherited by frontend children."""
    from genericagent import paths as ga_paths

    roots = ga_paths.project_roots(project=project, data_root=data_root)
    launch_path = str(ga_paths._normalized_path(launch or ga_paths.launch_cwd()))
    return {
        "GA_PROJECT_ROOT": str(roots.project_root),
        "GA_PROJECT_KEY": roots.project_key,
        "GA_PROJECT_DATA_DIR": str(roots.project_data_dir),
        "GA_DATA_DIR": str(roots.data_root),
        "GA_LAUNCH_CWD": launch_path,
        "GA_ORIGINAL_CWD": launch_path,
        "GA_TOOL_CWD": str(roots.project_root),
        "GA_WORKTREE_ROOT": str(roots.project_worktrees_dir),
    }

def _frontends() -> str:
    source = _source_project_dir()
    if source:
        return os.path.join(source, "frontends")
    return str(PACKAGE_PARENT / "frontends")

def _reflect():
    return os.path.join(PROJECT_DIR, "reflect")


def resolve_executable(command: str) -> str:
    """Resolve bare executable names for Windows .cmd/.exe launch stability."""
    if os.path.dirname(command) or command.startswith((".", os.sep)):
        return command
    return shutil.which(command) or command


def launch_frontend(cmd_parts, args=None):
    """启动前端/工具进程，并保留用户当前工作区作为 Agent workspace。"""
    full_cmd = []
    for i, part in enumerate(cmd_parts):
        part = part.replace("{PROJECT_DIR}", PROJECT_DIR)
        part = part.replace("{FRONTENDS}", _frontends())
        part = part.replace("{REFLECT}", _reflect())
        if i == 0:
            part = resolve_executable(part)
        full_cmd.append(part)

    # 插入额外参数
    if args:
        full_cmd.extend(args)

    # Keep the launcher quiet by default.  The Ink UI clears the terminal and
    # owns the first visible frame; a Python banner here flashes in PowerShell
    # before that frame and looks like a second startup line.
    # The Ink UI itself must start from the installed UI directory so Node can
    # resolve tsx/node_modules, while the Python bridge still receives the
    # user's original workspace through GA_PROJECT_ROOT.
    original_cwd = os.getcwd()
    env = os.environ.copy()
    try:
        from genericagent import paths as ga_paths
        env.update(project_environment(project=PROJECT_DIR, launch=original_cwd))
        env.setdefault("GA_CONFIG_DIR", str(ga_paths.user_config_dir()))
        env.setdefault("GA_DATA_DIR", str(ga_paths.user_data_dir()))
    except Exception:
        env["GA_PROJECT_ROOT"] = PROJECT_DIR
        env["GA_LAUNCH_CWD"] = original_cwd
        env["GA_ORIGINAL_CWD"] = original_cwd
    ui_cwd = os.path.dirname(os.path.dirname(_frontends()))
    try:
        ui_dir = os.path.join(_frontends(), "ink", "ui")
        os.chdir(ui_dir if os.path.isdir(ui_dir) else PROJECT_DIR)
    except OSError:
        pass
    proc = subprocess.Popen(full_cmd, env=env)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        sys.exit(0)
    finally:
        try:
            os.chdir(original_cwd)
        except OSError:
            pass


COMMANDS = {
    # Supported entry points.  `ga` with no subcommand launches `ga ink`.
    "ink": {
        "help": "启动 React/Ink 终端 UI",
        "desc": "通过 JSONL bridge 调用 Python 后端；这是默认且受支持的交互入口",
        "cmd": [
            "node",
            "{FRONTENDS}/ink/ui/node_modules/tsx/dist/cli.mjs",
            "{FRONTENDS}/ink/ui/src/main.tsx",
        ],
    },
    "cli": {
        "help": "启动轻量 Python CLI",
        "desc": "不起 Node UI，直接运行 genericagent.core.app",
        "cmd": ["python", "-m", "genericagent.core.app"],
    },
    "list": {
        "help": "列出支持的命令",
        "desc": "显示当前支持的 GA 入口",
        "cmd": None,
        "internal": True,
    },
    "status": {
        "help": "检查运行状态",
        "desc": "检查当前是否已有 GenericAgent 进程在运行",
        "cmd": None,
        "internal": True,
    },
    "update": {
        "help": "更新项目 (git pull + pip install)",
        "desc": "从 Git 拉取最新代码并更新核心依赖",
        "cmd": None,
        "internal": True,
    },
    "migrate": {
        "help": "显式迁移旧路径数据",
        "desc": "默认 dry-run；--apply 只复制，不删除 legacy 源",
        "cmd": None,
        "internal": True,
    },
}

# Kept as compatibility shims only.  They are not advertised by `ga list` and
# their dependencies live in opt-in `legacy-*` extras.  The supported product
# surface is `ga` / `ga ink`.
DEPRECATED_COMMANDS = {
    "gui": "PySide6 桌面入口已过时，请使用 `ga` 或 `ga ink`；仅需兼容时安装 `.[legacy-qt]`",
    "web": "Streamlit Web 入口已过时，请使用 `ga ink`；仅需兼容时安装 `.[legacy-ui]`",
    "launch": "pywebview 桌面壳已过时，请使用 `ga ink`；仅需兼容时安装 `.[legacy-ui]`",
    "tui": "Textual TUI 已过时，请使用 `ga ink`；仅需兼容时安装 `.[legacy-ui]`",
    "pet": "桌面宠物入口已过时，不属于 GA 发布面；仅需兼容时安装 `.[legacy-ui]`",
    "hub": "Hub 启动器已过时，不属于 GA 发布面",
    "configure": "配置向导尚未纳入当前两个入口的发布面；请直接编辑 `llm.yaml` / `mykey.py`",
}


def cmd_list():
    """展示所有可用命令"""
    print()
    frontend_cmds = [(k, v) for k, v in sorted(COMMANDS.items()) if v["cmd"] is not None]
    internal_cmds = [(k, v) for k, v in sorted(COMMANDS.items()) if v["cmd"] is None]

    print(f"  {'命令':20s}  {'说明'}")
    print(f"  {'━'*20}  {'━'*40}")
    for name, info in frontend_cmds:
        print(f"  {name:20s}  {info.get('help', info['desc'][:40])}")
    print()
    for name, info in internal_cmds:
        print(f"  {name:20s}  {info.get('help', info['desc'][:40])}")
    print()


def cmd_status():
    """检查进程状态"""
    import psutil
    running = [p for p in psutil.process_iter(['pid', 'name', 'cmdline'])
               if p.info['cmdline'] and any('genericagent.core.app' in c for c in p.info['cmdline'])]
    if running:
        print(f"[OK] 运行中: {len(running)} 个进程")
        for p in running:
            print(f"   PID {p.info['pid']} — {' '.join(p.info['cmdline'][:3])}")
    else:
        print("[IDLE] GenericAgent 进程未运行")


def cmd_update():
    """Update a source checkout; installed wheel users upgrade with pip."""
    source = _source_project_dir()
    if not source:
        print("[UPDATE] Installed wheel mode: use `python -m pip install --upgrade genericagent`.")
        return
    os.chdir(source)
    print("[UPDATE] git pull...")
    r = subprocess.run(["git", "pull"], capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
    print("[UPDATE] pip install...")
    r2 = subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."],
                        capture_output=True, text=True)
    print(r2.stdout[-500:] if r2.stdout else "")
    if r2.returncode != 0:
        print(r2.stderr[-500:])


def _cmd_migrate(args):
    """Run the explicit, copy-only path migration CLI.

    Commands:
      ga migrate paths [--apply] [--kinds sessions,workflows,named,teams] [--report]
                       [--team-map OLD_LIST_ID=TEAM_KEY] [--operator NAME]
      ga migrate claim <session.jsonl> [--apply]
    """
    import json
    from dataclasses import asdict
    from ga_cli import migrate_paths
    from genericagent import paths as ga_paths

    data_root = str(Path(os.environ.get("GA_DATA_DIR") or ga_paths.data_root()))
    if not args or args[0] in {"-h", "--help", "help"}:
        print("usage: ga migrate paths [--apply] [--kinds ...] [--report]")
        print("       ga migrate paths --team-map OLD_LIST_ID=TEAM_KEY")
        print("       ga migrate claim <session.jsonl> [--apply]")
        return 0

    action = args[0]
    if action == "paths":
        apply = "--apply" in args[1:]
        report = "--report" in args[1:]
        kinds = None
        team_map = {}
        operator = None
        values = args[1:]
        index = 0
        while index < len(values):
            value = values[index]
            if value == "--kinds" and index + 1 < len(values):
                kinds = {part.strip() for part in values[index + 1].split(",") if part.strip()}
                index += 2
                continue
            if value == "--team-map" and index + 1 < len(values):
                raw = values[index + 1]
                old_id, separator, team_key = raw.partition("=")
                if not separator or not old_id or not team_key:
                    print("--team-map must be OLD_LIST_ID=TEAM_KEY", file=sys.stderr)
                    return 2
                team_map[old_id] = team_key
                index += 2
                continue
            if value == "--operator" and index + 1 < len(values):
                operator = values[index + 1]
                index += 2
                continue
            index += 1
        result = migrate_paths.run_migration(
            PROJECT_DIR,
            data_root,
            apply=apply,
            kinds=kinds,
            report=report,
            team_map=team_map,
            operator=operator,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("conflicts") or result.get("apply_blocked") else 0

    if action == "claim":
        if len(args) < 2:
            print("usage: ga migrate claim <session.jsonl> [--apply]", file=sys.stderr)
            return 2
        source = args[1]
        apply = "--apply" in args[2:]
        result = migrate_paths.claim_legacy_session(
            source,
            project_root=PROJECT_DIR,
            data_root=data_root,
            apply=apply,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 1 if result.status == "conflict" else 0

    print(f"[ERROR] unknown migrate action: {action}", file=sys.stderr)
    return 2


def main():
    parser = argparse.ArgumentParser(
        prog="ga",
        description="GenericAgent 全局命令入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\n            示例:
              ga                   启动 React/Ink TUI（默认）
              ga ink               显式启动 React/Ink TUI
              ga cli               启动轻量 Python CLI
              ga list              列出支持的命令
              ga status            检查运行状态
              ga update            更新项目
        """),
    )
    parser.add_argument("command", nargs="?", help="命令名")
    parser.add_argument("args", nargs="*", help="子命令参数")
    parser.add_argument("-v", "--version", action="store_true", help="显示版本")

    args, unknown = parser.parse_known_args()

    if args.version:
        print("GenericAgent v0.1.0")
        return

    cmd = args.command

    if not cmd:
        cmd = "ink"

    if cmd == "help":
        parser.print_help()
        print("\n--- 命令列表 ---")
        cmd_list()
        return

    if cmd == "list":
        cmd_list()
        return

    if cmd == "status":
        cmd_status()
        return

    if cmd == "update":
        cmd_update()
        return

    if cmd == "migrate":
        raise SystemExit(_cmd_migrate(list(args.args) + list(unknown)))

    if cmd in DEPRECATED_COMMANDS:
        print(f"[DEPRECATED] {cmd}: {DEPRECATED_COMMANDS[cmd]}")
        sys.exit(2)

    if cmd not in COMMANDS:
        print(f"[ERROR] 未知命令: {cmd}")
        print("   支持的命令: ga / ga ink / ga cli / ga list / ga status / ga update")
        sys.exit(1)

    info = COMMANDS[cmd]

    # 内置命令走内部逻辑
    if info.get("internal"):
        print(f"[ERROR] 命令 {cmd} 没有配置启动命令")
        sys.exit(1)

    extra = list(args.args) + unknown

    # === 处理命令特有 flags ===
    cmd_parts = list(info["cmd"])

    # 处理 flags (如 --native)
    flags = info.get("flags", {})
    for flag_name, flag_info in flags.items():
        if flag_name in extra:
            cmd_parts = list(flag_info["cmd"])
            extra.remove(flag_name)
            break

    launch_frontend(cmd_parts, extra if extra else None)


if __name__ == "__main__":
    main()
