import os, sys, threading, queue, time, json, re, random, locale, copy, subprocess
from genericagent import paths
_PROCESS_START_MONOTONIC_NS = time.monotonic_ns()


def _startup_task_dir_from_argv(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    def value_for(name):
        for index, value in enumerate(argv):
            if value == name and index + 1 < len(argv):
                return argv[index + 1]
            if value.startswith(name + '='):
                return value.split('=', 1)[1]
        return None

    task_name = value_for('--task')
    if not task_name:
        return None
    explicit_task_dir = value_for('--task_dir')
    if explicit_task_dir:
        return os.path.abspath(explicit_task_dir)
    # New child protocol: state_root owns task state. The old task_root
    # remains an exact legacy alias and is never treated as project_root.
    state_root = value_for('--state_root') or value_for('--task_root') or str(paths.RUNTIME_ROOT)
    return os.path.join(os.path.abspath(state_root), 'temp', task_name)


def _record_startup_phase(phase, task_dir=None, **details):
    """Append a task-local marker without touching the shared event bus."""
    task_dir = str(task_dir or _startup_task_dir_from_argv() or '')
    if not task_dir:
        return None
    elapsed_ms = round((time.monotonic_ns() - _PROCESS_START_MONOTONIC_NS) / 1_000_000, 3)
    row = {
        'schema_version': 1,
        'type': 'startup_phase',
        'phase': str(phase),
        'pid': os.getpid(),
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'elapsed_ms': elapsed_ms,
        **details,
    }
    try:
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, 'startup.jsonl'), 'a', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
            f.flush()
    except OSError:
        return None
    return row


_record_startup_phase('process_entry')
paths.ensure_runtime_dirs()
os.environ.setdefault('GA_LANG', 'zh' if any(k in (locale.getlocale()[0] or '').lower() for k in ('zh', 'chinese')) else 'en')
def _configure_stdio_utf8():
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        elif hasattr(stream, 'reconfigure'):
            try: stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception: stream.reconfigure(errors='replace')
_configure_stdio_utf8()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from genericagent.llm.core import NativeClaudeSession, NativeOAISession
from genericagent.llm.client import load_clients_from_yaml
from genericagent.core.loop import agent_runner_loop
from genericagent.core.handler import GenericAgentHandler, smart_format, get_global_memory, format_error, consume_file
from genericagent.core.skills import build_skill_prompt
from genericagent.core import transcripts as session_transcript
from genericagent.core import goal as goal_core
from genericagent.core import goal_evaluator
from genericagent.core import goal_proposal
from genericagent.core import goal_metadata
from genericagent.core import goal_telemetry
from genericagent.core import goal_nudge
from genericagent.media import image_codec
try:
    from genericagent.core.context import compact_agent_context, replace_log_with_compact_history
except Exception:  # 压缩核心导入失败时降级：/compact 报不可用，不影响主流程
    compact_agent_context = None
    replace_log_with_compact_history = None
from genericagent.core.project_instructions import build_ga_project_instructions, load_base_system_prompt
from genericagent.agents.state import append_jsonl_event, append_parent_inbox_event, atomic_write_json, now_iso, read_json_or_none, sha256_file
from genericagent.agents.prompts import build_agent_role_usage_hint

_record_startup_phase('imports_complete')

script_dir = str(paths.PROJECT_ROOT)
# llm.yaml 热重载：记录上次成功加载的路径与 mtime，未变则跳过重建。
_llm_yaml_path: str | None = None
_llm_yaml_mtime_ns: int | None = None
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}
STREAM_FLUSH_CHARS = 16


def _extract_image_paths(text):
    paths = []
    def add_path(p):
        path = p if os.path.isabs(p) else os.path.join(script_dir, p)
        if os.path.isfile(path) and path not in paths:
            paths.append(path)

    for raw in re.findall(r'"([^"]+\.(?:png|jpe?g|webp|gif|bmp))"|\'([^\']+\.(?:png|jpe?g|webp|gif|bmp))\'', text or '', re.I):
        p = next((x for x in raw if x), '')
        if p: add_path(p)

    for m in re.finditer(r'(?:[A-Za-z]:[\\/]|/)[^\r\n"\']+?\.(?:png|jpe?g|webp|gif|bmp)', text or '', re.I):
        add_path(m.group(0).strip())

    for m in re.finditer(r'\S+\.(?:png|jpe?g|webp|gif|bmp)', text or '', re.I):
        add_path(m.group(0).strip())
    return paths


def _image_block(path):
    """兼容旧调用：走有界 codec。"""
    enc = image_codec.encode_image_for_prompt(path)
    return image_codec.build_image_block(enc)


def resolve_reasoning_effort_for_model(current, supported, default):
    """Resolve a session effort against target-model capabilities.

    ``supported is None`` means legacy/unknown metadata, so the value is
    preserved rather than replaced with an invented global default.
    """
    if supported is None:
        return current
    if current in supported:
        return current
    if default in supported:
        return default
    return supported[0] if supported else None


def _build_user_content_with_images(text, images=None):
    extra = _extract_image_paths(text)
    return image_codec.build_user_content_with_images(text, images=images, extra_paths=extra)


def supports_image_input(llmclient):
    """当前 backend 是否支持把本地图作为原生多模态 user content 发送。"""
    backend = getattr(llmclient, 'backend', None)
    backend = getattr(backend, 'primary', backend)
    if backend is None:
        return False
    if bool(getattr(backend, 'native_image_input', False)):
        return True
    if bool(getattr(backend, 'supports_image_input', False)):
        return True
    # Native Claude Messages 原生支持 image block；OAI 仍要求显式 flag/capability
    if isinstance(backend, NativeClaudeSession) and not isinstance(backend, NativeOAISession):
        # 允许 cfg 显式关掉
        if 'native_image_input' in getattr(backend, '__dict__', {}):
            return bool(getattr(backend, 'native_image_input'))
        return True
    if isinstance(backend, NativeOAISession):
        return bool(getattr(backend, 'native_image_input', False))
    return False


def _native_image_input_enabled(llmclient):
    """兼容旧名；语义升级为 supports_image_input。"""
    return supports_image_input(llmclient)


def _is_image_content_block(block) -> bool:
    """Claude image / OAI image_url / Responses input_image 均算原生图。"""
    if not isinstance(block, dict):
        return False
    t = block.get("type")
    if t in ("image", "input_image"):
        return True
    if t == "image_url":
        return True
    # 少数适配器把图塞在 source.type=base64
    src = block.get("source")
    if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
        return True
    return False


def count_image_blocks_in_content(content) -> int:
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content if _is_image_content_block(b))


def content_has_native_images(content) -> bool:
    return count_image_blocks_in_content(content) > 0


def history_has_native_images(history, *, max_messages: int = 24) -> bool:
    """扫描近期 history 是否已有原生 image block（用于「重试」等无新附件轮次）。"""
    if not isinstance(history, list) or not history:
        return False
    for msg in history[-max_messages:]:
        if not isinstance(msg, dict):
            continue
        if content_has_native_images(msg.get("content")):
            return True
    return False


def history_native_image_count(history, *, max_messages: int = 24) -> int:
    if not isinstance(history, list) or not history:
        return 0
    n = 0
    for msg in history[-max_messages:]:
        if isinstance(msg, dict):
            n += count_image_blocks_in_content(msg.get("content"))
    return n


def transcript_delta_anchor(agent):
    """Return the cheapest safe anchor for recording this turn's transcript delta.

    v2 sessions persist only the appended tail, so the pre-turn list length is
    sufficient.  v1 still needs its legacy full snapshot for compatibility.
    """
    history = backend_history(getattr(agent, "llmclient", None))
    session_path = getattr(agent, "session_path", None)
    if session_path and session_transcript._detect_session_version(session_path) == 1:
        return session_transcript.current_backend_history(agent)
    return len(history)


def backend_history(llmclient):
    backend = getattr(llmclient, "backend", None)
    backend = getattr(backend, "primary", backend)
    hist = getattr(backend, "history", None) if backend is not None else None
    return hist if isinstance(hist, list) else []


_VISION_FOLLOWUP_RE = re.compile(
    r"(?is)("
    r"重试|再试|再看|重看|再描述|重新描述|"
    r"这图|那图|截图|图片|图里|图中|附图|贴图|识图|看图|"
    r"是什么|啥意思|描述|说说|看看|"
    r"retry|again|describe|what(?:'s| is)? (?:in |on )?(?:the )?(?:image|screenshot|picture)|"
    r"look at (?:the )?(?:image|screenshot|picture)"
    r")"
)


def user_query_looks_like_vision_followup(text: str) -> bool:
    """用户本轮像在追问已贴图片（含「重试」），而非新的纯代码任务。"""
    t = (text or "").strip()
    if not t:
        return False
    if _VISION_FOLLOWUP_RE.search(t):
        return True
    # 极短跟进（如「呢」「呢？」）且不含路径/代码气味
    if len(t) <= 12 and not re.search(r"[\\/]|\.py\b|\.ts\b|def |class |import ", t):
        return True
    return False


def build_vision_direct_answer_sys_prompt(*, image_count: int = 0, via: str = "current") -> str:
    """强制 vision 直答的 system 补丁。

    对齐：
    - Codex view_image：仅当图「尚未」挂在 thread 时才用工具加载；已在上下文则直接看图
      （view_image_spec: "only use if ... image isn't already attached to the thread context"）。
    - Claude Code：粘贴图作为原生 image block 进入 user content，不先走 OCR/搜仓库。

    via:
      - current: 本轮 user content 新挂了 image block
      - history: 本轮无新图，但近期 history 已有原生图（如用户只说「重试」）
    """
    n = max(0, int(image_count or 0))
    n_bit = f"共约 {n} 张原生图片。" if n else "消息/历史中已有原生图片。"
    where = (
        "本条用户消息的 content 里已包含 type=image / image_url 的多模态块（文本里的 [Image #N] 只是 UI 占位符，不是要你去仓库搜索的符号）。"
        if via == "current"
        else "近期对话 history 里已包含原生多模态图片块；用户本轮可能只是「重试/再看/补充一句」，图仍在上下文中。"
    )
    return (
        "\n[Native vision — direct answer]\n"
        f"{where}{n_bit}\n"
        "你必须优先用视觉能力直接阅读这些图片像素并回答用户，遵守：\n"
        "1. 第一轮优先直接作答：描述/对比/回答图中问题；多图时按 [Image #1]、[Image #2]… 或出现顺序分别说明，勿合并成含糊一句。\n"
        "2. 禁止为了「理解 [Image #N] 占位符是什么」而去 code_run / 搜索 ink-ui / 读 imageAttachments.ts；"
        "那是前端附件标签，不等于图片内容。\n"
        "3. 禁止优先 OCR、file_read 本地路径、vision_api、全屏截图；图已在上下文中（同 Codex："
        "已在 thread 中的图不要再 view_image/重复加载）。\n"
        "4. 仅当用户明确要求改代码/查实现，或图片完全无法辨认时，才改用工具；"
        "无法辨认时先说明看不清，再问是否重传或改路径。\n"
        "5. 不要假装看见图却去复述仓库代码；视觉结论必须来自图片本身。\n"
    )


def should_inject_vision_direct_answer(
    *,
    can_image: bool,
    initial_content,
    history,
    user_text: str,
) -> tuple[bool, str, int]:
    """返回 (是否注入, via, image_count)。"""
    if not can_image:
        return False, "", 0
    n_cur = count_image_blocks_in_content(initial_content)
    if n_cur > 0:
        return True, "current", n_cur
    n_hist = history_native_image_count(history)
    if n_hist > 0 and user_query_looks_like_vision_followup(user_text):
        return True, "history", n_hist
    return False, "", 0


def should_flush_display_delta(full_resp, last_pos, chunk):
    return len(full_resp) - last_pos >= STREAM_FLUSH_CHARS or re.search(r'LLM Running \(Step \d+\) \.\.\.', chunk) is not None


def normalize_display_assistant_text(full_resp: str) -> str:
    """Idempotent display-only markup rewrites (tests / non-streaming callers).

    Prefer not using this on the live stream path after partial flushes — inserting
    characters before `last_pos` corrupts incremental deltas. The agent run loop
    ships raw stream text and leaves spacing to the frontend formatter.
    """
    if not full_resp:
        return full_resp
    text = full_resp
    if '</summary>' in text:
        text = text.replace('</summary>\n\n', '</summary>')
        text = text.replace('</summary>', '</summary>\n\n')
    if '</file_content>' in text:
        # Wrap bare tags only; leave already-fenced blocks untouched (idempotent).
        parts: list[str] = []
        cursor = 0
        for match in re.finditer(r'<file_content>\s*(.*?)\s*</file_content>', text, flags=re.DOTALL):
            start, end = match.span()
            before = text[max(0, start - 5):start]
            after = text[end:end + 5]
            already_fenced = before.endswith('````\n') or before.endswith('````') and after.startswith('\n````')
            # Also detect fenced form "````\n<file_content>...\n````"
            window_before = text[max(0, start - 6):start]
            window_after = text[end:end + 6]
            already_fenced = window_before.endswith('````\n') and window_after.startswith('\n````')
            parts.append(text[cursor:start])
            if already_fenced:
                parts.append(match.group(0))
            else:
                body = match.group(1)
                parts.append(f'\n````\n<file_content>\n{body}\n</file_content>\n````')
            cursor = end
        parts.append(text[cursor:])
        text = ''.join(parts)
    return text


def _apply_subagent_role_schema(tools_schema, root_dir, *, language="en"):
    """Publish the role catalog, with capability notes, on ``spawn_agent``.

    Listing bare names is not enough: the choice happens before any catalog is
    shown, so a caller that sees only ``explore, plan, review`` has no way to know
    which of them may write. Step-Code solves this by deriving the tool
    description from the agent catalog itself
    (``step-subagent-agents.ts::formatBuiltinAgentGuidance``); the notes here come
    from each role's own ``permission_profile`` for the same reason.
    """

    from genericagent.agents.roles import SubagentRoleRegistry, format_role_catalog

    roles = sorted(
        SubagentRoleRegistry(root_dir).list_roles(),
        key=lambda role: str(getattr(role, "name", "") or ""),
    )
    role_names = [str(getattr(role, "name", "") or "") for role in roles if getattr(role, "name", None)]
    for tool in tools_schema:
        function = tool.get("function") or {}
        if function.get("name") != "spawn_agent":
            continue
        properties = (function.get("parameters") or {}).get("properties") or {}
        if not role_names:
            properties.pop("agent_type", None)
            return
        agent_type = properties.setdefault("agent_type", {"type": "string"})
        agent_type["enum"] = role_names
        configured = ", ".join(role_names)
        catalog = format_role_catalog(roles, language=language)
        if language == "zh":
            agent_type["description"] = (
                f"可选的预配置角色名；当前可用值：{configured}。{catalog}"
                "只能使用此列表中的精确名称，也可省略以启动通用子智能体；该字段不是自由标签。"
                "审查 / 审计 / 探索类工作优先用只读角色，只有通用子智能体或声明了写权限的项目角色才能改文件。"
            )
        else:
            agent_type["description"] = (
                f"Optional preconfigured role name. Available values: {configured}. {catalog} "
                "Use one exact listed value, or omit this field to spawn a generic subagent; "
                "this is not a free-form label. Prefer a read-only role for review, audit or "
                "exploration work; only a generic spawn (or a project role that declares write "
                "access) can modify files."
            )
        return


def load_tool_schema(suffix='', include_mcp_tools=True):
    global TOOLS_SCHEMA
    schema_path = os.path.join(script_dir, f'assets/tools_schema{suffix}.json')
    if not os.path.isfile(schema_path):
        schema_path = str(paths.BUNDLED_ASSETS_DIR / f'tools_schema{suffix}.json')
    with open(schema_path, 'r', encoding='utf-8') as f:
        TS = f.read()
    TOOLS_SCHEMA = json.loads(TS if os.name == 'nt' else TS.replace('powershell', 'bash'))
    _apply_subagent_role_schema(
        TOOLS_SCHEMA,
        script_dir,
        language="zh" if suffix == "_cn" else "en",
    )
    if not include_mcp_tools:
        return
    try:
        from genericagent.mcp.runtime import discover_mcp_tools_cached_fast
        append_mcp_tools(TOOLS_SCHEMA, discover_mcp_tools_cached_fast())
        start_mcp_schema_warmup()
    except Exception as e:
        if os.environ.get("GA_MCP_DEBUG"):
            print(f"[WARN] MCP tool discovery failed: {e}")


_MCP_SCHEMA_WARMUP_STARTED = False


def append_mcp_tools(schema, tools):
    """Append not-yet-present MCP tools in place so a running turn can pick them up."""
    existing = {t.get("function", {}).get("name") for t in schema}
    appended = []
    for tool in tools or []:
        name = tool.get("function", {}).get("name")
        if name and name not in existing:
            schema.append(tool)
            existing.add(name)
            appended.append(name)
    return appended


def _on_background_mcp_discovery(tools, complete=True):
    """Background discovery finished: extend the live schema without blocking a turn."""
    try:
        append_mcp_tools(TOOLS_SCHEMA, tools)
    except Exception as e:
        if os.environ.get("GA_MCP_DEBUG"):
            print(f"[WARN] MCP schema append failed: {e}")


def start_mcp_schema_warmup():
    """Discover MCP tools once, off the critical path, and append them when ready.

    Every user turn used to block on MCP discovery (measured 16s cold, and a
    partial cache re-triggered it every 60s). The warmup turns that into a
    one-time background cost: turns read whatever is already known and the
    schema grows in place when a server finally answers.
    """
    global _MCP_SCHEMA_WARMUP_STARTED
    if _MCP_SCHEMA_WARMUP_STARTED:
        return None
    _MCP_SCHEMA_WARMUP_STARTED = True
    try:
        from genericagent.mcp.runtime import on_mcp_discovery_complete, start_background_discovery
        on_mcp_discovery_complete(_on_background_mcp_discovery)
        return start_background_discovery()
    except Exception as e:
        if os.environ.get("GA_MCP_DEBUG"):
            print(f"[WARN] MCP warmup failed: {e}")
        return None
load_tool_schema(include_mcp_tools=False)

lang_suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
# Runtime state belongs to USER_DATA_DIR, not the user's project checkout.
# `script_dir` remains the workspace root used for tools and project docs.
mem_dir = str(paths.MEMORY_DIR)
if not os.path.exists(mem_dir): os.makedirs(mem_dir)
mem_txt = os.path.join(mem_dir, 'global_mem.txt')
if not os.path.exists(mem_txt): open(mem_txt, 'w', encoding='utf-8').write('# [Global Memory - L2]' + chr(10))
mem_insight = os.path.join(mem_dir, 'global_mem_insight.txt')
if not os.path.exists(mem_insight):
    t = os.path.join(str(paths.ASSETS_DIR), f'global_mem_insight_template{lang_suffix}.txt')
    if not os.path.exists(t):
        t = os.path.join(str(paths.BUNDLED_ASSETS_DIR), f'global_mem_insight_template{lang_suffix}.txt')
    open(mem_insight, 'w', encoding='utf-8').write(open(t, encoding='utf-8').read() if os.path.exists(t) else '')
cdp_cfg = os.path.join(str(paths.ASSETS_DIR), 'tmwd_cdp_bridge/config.js')
if not os.path.exists(cdp_cfg):
    try:
        os.makedirs(os.path.dirname(cdp_cfg), exist_ok=True)
        open(cdp_cfg, 'w', encoding='utf-8').write(f"const TID = '__ljq_{hex(random.randint(0, 99999999))[2:8]}';")
    except Exception as e: print(f'[WARN] CDP config init failed: {e} — advanced web features (tmwebdriver) will be unavailable.')

# Render order of the system prompt. The pieces are kept separate (rather than
# concatenated inline) so the prompt is diffable: pi splits its system prompt
# into named sections and reports which ones changed
# (``diffSystemPromptSections``) instead of re-sending the whole thing blind.
SYSTEM_PROMPT_SECTION_ORDER = (
    "base",
    "date",
    "project",
    "notifications",
    "memory",
    "skills",
    "role_hint",
    "permission_mode",
)


def build_system_prompt_sections(agent=None):
    """Named prompt sections in render order; ``render_system_prompt`` joins them."""

    project_root = str(getattr(agent, "project_root", None) or script_dir)
    current_dir = str(getattr(agent, "tool_cwd", None) or project_root)
    is_subagent = bool(getattr(agent, 'task_dir', None))
    notifications = ""
    if not is_subagent:
        try:
            from genericagent.agents.notifications import build_subagent_notifications_prompt
            notifications = build_subagent_notifications_prompt(project_root)
        except Exception:
            pass
    return {
        "base": load_base_system_prompt(project_root, lang_suffix),
        "date": f"\nToday: {time.strftime('%Y-%m-%d %a')}\n",
        "project": build_ga_project_instructions(project_root, current_dir),
        "notifications": notifications,
        "memory": get_global_memory(agent),
        "skills": build_skill_prompt(),
        "role_hint": "\n" + build_agent_role_usage_hint(
            is_subagent=is_subagent,
            team_mode=bool(getattr(agent, "capability_team_mode", False)),
            lang_suffix=lang_suffix,
        ) + "\n",
        "permission_mode": build_permission_mode_hint(getattr(agent, 'permission_mode', None)),
    }


def render_system_prompt(sections):
    """Join the sections in ``SYSTEM_PROMPT_SECTION_ORDER`` (the only renderer)."""

    return "".join(str((sections or {}).get(name) or "") for name in SYSTEM_PROMPT_SECTION_ORDER)


def diff_system_prompt_sections(previous, current):
    """Names of the sections whose text changed, in render order."""

    previous = previous if isinstance(previous, dict) else {}
    current = current if isinstance(current, dict) else {}
    return [
        name
        for name in SYSTEM_PROMPT_SECTION_ORDER
        if str(previous.get(name) or "") != str(current.get(name) or "")
    ]


def note_system_prompt_sections(agent, sections):
    """Remember this turn's sections and return the names that changed.

    The system prompt is rebuilt for every turn, so an unchanged prompt is a
    cache hit and a changed one is a miss the model must re-read. Reporting the
    section names turns "why did the cache miss" into a one-line answer instead
    of a guess (pi keeps the same miss statistics).
    """

    previous = getattr(agent, "_last_prompt_sections", None)
    try:
        setattr(agent, "_last_prompt_sections", sections)
    except Exception:
        pass
    if previous is None:
        return []
    return diff_system_prompt_sections(previous, sections)


def get_system_prompt(agent=None):
    return render_system_prompt(build_system_prompt_sections(agent))


def build_permission_mode_hint(mode):
    '''非 full_access 档时告知模型当前权限边界，避免反复调用会被拒的写/执行工具空转。

    full_access（默认）不注入任何内容，保持历史「工具全开」提示词不变。
    '''
    try:
        from genericagent.core.permissions.policy import ASK, FULL_ACCESS, READ_ONLY, normalize_permission_mode
    except Exception:
        return ""
    mode = normalize_permission_mode(mode)
    if mode == FULL_ACCESS:
        return ""
    if mode == READ_ONLY:
        return (
            "\n[Permission: Read Only] 当前为只读档。写文件 / 打补丁 / 执行代码 / 执行 JS 等"
            "有副作用的工具会被拒绝（返回 permission deny）。只做读取、检索、分析并直接向用户"
            "汇报；需要改动时说明将要做什么并请用户切换到 Ask 或 Full Access，不要反复重试被拒的工具。\n"
        )
    if mode == ASK:
        return (
            "\n[Permission: Ask for approval] 当前为审批档。读取类工具直接可用；写文件 / 打补丁 /"
            "执行代码 / 执行 JS 等有副作用的工具会阻塞等待用户 accept/deny。"
            "被拒绝时不要盲目重试同一写/执行；调整方案或请用户改用 Full Access。\n"
        )
    return ""

class GenericAgent:
    def __init__(self):
        os.makedirs(str(paths.TEMP_DIR), exist_ok=True)
        # Slice D3：进程内首次构造 agent 时 GC 自产图片缓存（best-effort）
        _record_startup_phase('agent_image_gc_started')
        image_gc_status, image_gc_error = 'ok', None
        try:
            from genericagent.media.image_gc import maybe_gc_ga_images_on_startup
            maybe_gc_ga_images_on_startup(quiet=True)
        except Exception as e:
            image_gc_status, image_gc_error = 'error', f'{type(e).__name__}: {e}'
        _record_startup_phase('agent_image_gc_complete', status=image_gc_status, error=image_gc_error)
        self.lock = threading.Lock()
        self.task_dir = None
        self.history = []; self.handler = None;
        self.task_queue = queue.Queue()
        self.is_running = False; self.stop_sig = False
        self.llm_no = 0;  self.inc_out = False; self.verbose = True
        self.peer_hint = True
        self.is_subagent = False
        self.capability_profile = None
        self.capability_allow_delegation = False
        self.capability_team_mode = False
        # 三档权限：默认 full_access（等于历史「工具全开」行为），可切到 ask / read_only
        _record_startup_phase('agent_permission_init_started')
        from genericagent.core.permissions.policy import DEFAULT_PERMISSION_MODE
        self.permission_mode = DEFAULT_PERMISSION_MODE
        self.subagent_permission_policy = None
        # ask 档阻塞审批 runtime（可无 emit = headless deny）；bridge 会 set_emit
        permission_status, permission_error = 'ok', None
        try:
            from genericagent.core.permissions.runtime import PermissionRuntime
            self.permission_runtime = PermissionRuntime()
        except Exception as e:
            self.permission_runtime = None
            permission_status, permission_error = 'error', f'{type(e).__name__}: {e}'
        _record_startup_phase('agent_permission_init_complete', status=permission_status, error=permission_error)
        self.log_path = str(paths.TEMP_DIR / f'model_responses/model_responses_{int(time.time()*1e6)%1000000:06d}.txt')
        _record_startup_phase('agent_model_config_started')
        self.load_llm_sessions()
        _record_startup_phase('agent_model_config_complete', llm_count=len(self.llmclients), active_llm_no=self.llm_no)
        self.session_id = None
        self.session_path = None
        self.session_turn_id = 0
        # Session goal mode (Stage F). Phase 0: state + /goal command only.
        self.active_goal = None
        self._queued_goal_origin = None
        self._last_goal_metadata = None
        self.hook_runtime = None
        # Phase 4: one-keypress approval runtime for model-proposed goals.
        try:
            self.goal_proposal_runtime = goal_proposal.GoalProposalRuntime()
        except Exception as e:
            self.goal_proposal_runtime = None
            print(f"[WARN] Failed to init goal_proposal_runtime: {e}")
        identity = session_transcript.identity_from_environment()
        if identity:
            self.original_cwd = identity.get("original_cwd")
            self.project_root = identity.get("project_root")
            self.launch_cwd = identity.get("launch_cwd")
            self.project_key = identity.get("project_key")
            self.data_root = identity.get("data_root")
            self.tool_cwd = self.project_root
        else:
            self.original_cwd = os.getcwd()
            self.project_root = str(paths.PROJECT_ROOT)
            self.launch_cwd = str(paths.launch_cwd())
            self.project_key = paths.project_key(self.project_root)
            self.data_root = str(paths.data_root())
            self.tool_cwd = self.project_root
        _record_startup_phase('agent_session_init_started')
        session_status, session_error = 'ok', None
        try:
            session_transcript.ensure_agent_session(self, identity=identity)
        except Exception as e:
            session_status, session_error = 'error', f'{type(e).__name__}: {e}'
            print(f"[WARN] Failed to initialize session transcript: {e}")
        _record_startup_phase('agent_session_init_complete', status=session_status, error=session_error)
        try:
            from genericagent.hooks.runtime import run_hook_event
            run_hook_event(self, 'SessionStart', {"source": "startup"})
        except Exception as e:
            print(f"[WARN] SessionStart hooks failed: {e}")

    def load_llm_sessions(self):
        """阶段 3：只从 llm.yaml 构造会话列表（profiles + mixin），不再读 mykey.py。

        /model 列表项名 = profile 名（backend.name）；默认选中 active_profile。
        yaml 未变且已加载过则跳过重建，保留当前 llm_no 与 history。
        """
        global _llm_yaml_path, _llm_yaml_mtime_ns
        # 热重载：文件未变则复用已有 clients。
        if hasattr(self, "llmclients") and self.llmclients and _llm_yaml_path:
            try:
                if os.path.exists(_llm_yaml_path) and os.stat(_llm_yaml_path).st_mtime_ns == _llm_yaml_mtime_ns:
                    return
            except OSError:
                pass
        try:
            oldhistory = self.llmclient.backend.history
        except Exception:
            oldhistory = None
        try:
            old_name = self.get_llm_name() if hasattr(self, "llmclient") else None
        except Exception:
            old_name = None

        clients, active_index, cfg_path, mtime_ns = load_clients_from_yaml()
        _llm_yaml_path, _llm_yaml_mtime_ns = cfg_path, mtime_ns
        # Rebuilding the clients orphans the previous pooled HTTP sessions.
        for old_client in (getattr(self, "llmclients", None) or []):
            try:
                from genericagent.llm.core import close_http_session
                close_http_session(getattr(old_client, "backend", None))
            except Exception:
                pass
        self.llmclients = clients

        # 尽量按「上次选中的 profile 名」恢复下标；首次加载用 active_profile。
        index = active_index
        if old_name:
            for i, c in enumerate(clients):
                try:
                    if self.get_llm_name(c) == old_name:
                        index = i
                        break
                except Exception:
                    pass
        self.llm_no = index % len(self.llmclients)
        self.llmclient = self.llmclients[self.llm_no]
        if oldhistory:
            try:
                self.llmclient.backend.history = oldhistory
            except Exception:
                pass
        print(
            f"[Info] LLM sessions from {cfg_path}: "
            f"{len(self.llmclients)} profile(s), active=#{self.llm_no} {self.get_llm_name()}"
        )
    
    def next_llm(self, n=-1):
        self.load_llm_sessions()
        index = ((self.llm_no + 1) if n < 0 else int(n)) % len(self.llmclients)
        result = self._switch_llm_index(index)
        if not result.get("ok"):
            raise Exception(result.get("message") or "model switch failed")

    @staticmethod
    def _backend_for_client(client):
        backend = getattr(client, "backend", client)
        return getattr(backend, "primary", backend)

    def _llm_descriptor(self, index, client, current=False):
        backend = self._backend_for_client(client)
        efforts = getattr(backend, "reasoning_efforts", None)
        known = bool(getattr(backend, "reasoning_capabilities_known", efforts is not None))
        return {
            "index": int(index),
            "name": str(self.get_llm_name(client)),
            "current": bool(current),
            "reasoningEfforts": list(efforts) if efforts is not None else [],
            "reasoningEffortKnown": known,
            "defaultReasoningEffort": getattr(backend, "default_reasoning_effort", None),
            "reasoningEffort": getattr(backend, "reasoning_effort", None),
        }

    def current_llm_descriptor(self):
        self.load_llm_sessions()
        return self._llm_descriptor(self.llm_no, self.llmclient, current=True)

    def select_llm(self, selector, reasoning_effort=None):
        self.load_llm_sessions()
        selector = str(selector or "").strip()
        if not selector:
            return {"ok": False, "code": "empty", "message": "model selector is empty"}
        if selector.isdigit():
            return self._switch_llm_index(int(selector), reasoning_effort=reasoning_effort)
        lowered = selector.lower()
        # Prefer an exact profile/display name when a chat variant reuses the
        # same API model (for example deepseek-v4.1-flash and its -chat
        # profile). Only fall back to an exact API-model match, then substring
        # matching, when there is no exact profile candidate.
        exact_profile_matches = [
            i for i, client in enumerate(self.llmclients)
            if lowered == self.get_llm_name(client).lower()
            or lowered == getattr(client.backend, "name", "").lower()
        ]
        if len(exact_profile_matches) == 1:
            return self._switch_llm_index(exact_profile_matches[0], reasoning_effort=reasoning_effort)
        if len(exact_profile_matches) > 1:
            return {"ok": False, "code": "ambiguous", "message": f"ambiguous model selector: {selector}"}
        exact_model_matches = [
            i for i, client in enumerate(self.llmclients)
            if lowered == getattr(client.backend, "model", "").lower()
        ]
        if len(exact_model_matches) == 1:
            return self._switch_llm_index(exact_model_matches[0], reasoning_effort=reasoning_effort)
        if len(exact_model_matches) > 1:
            return {"ok": False, "code": "ambiguous", "message": f"ambiguous model selector: {selector}"}
        matches = [
            i for i, client in enumerate(self.llmclients)
            if lowered in self.get_llm_name(client).lower()
            or lowered in getattr(client.backend, "model", "").lower()
            or lowered in getattr(client.backend, "name", "").lower()
        ]
        if len(matches) == 1:
            return self._switch_llm_index(matches[0], reasoning_effort=reasoning_effort)
        if not matches:
            return {"ok": False, "code": "not_found", "message": f"model not found: {selector}"}
        return {"ok": False, "code": "ambiguous", "message": f"ambiguous model selector: {selector}"}

    def _switch_llm_index(self, index, reasoning_effort=None):
        self.load_llm_sessions()
        if not (0 <= index < len(self.llmclients)):
            return {"ok": False, "code": "out_of_range", "message": f"model index out of range: {index}"}
        lastc = self.llmclient
        last_backend = self._backend_for_client(lastc)
        target_client = self.llmclients[index]
        target_backend = self._backend_for_client(target_client)
        target_efforts = getattr(target_backend, "reasoning_efforts", None)
        target_known = bool(getattr(target_backend, "reasoning_capabilities_known", target_efforts is not None))
        requested = None if reasoning_effort is None else str(reasoning_effort).strip().lower()
        if requested is not None:
            if not target_known:
                return {
                    "ok": False,
                    "code": "reasoning_capabilities_unknown",
                    "message": "目标模型未声明可选思考等级",
                }
            if requested not in target_efforts:
                return {
                    "ok": False,
                    "code": "unsupported_reasoning_effort",
                    "message": f"目标模型不支持 {requested}；支持：{', '.join(target_efforts)}",
                }
        source_effort = getattr(last_backend, "reasoning_effort", None)
        target_current = getattr(target_backend, "reasoning_effort", None)
        if requested is not None:
            next_effort = requested
        elif target_known:
            next_effort = resolve_reasoning_effort_for_model(
                source_effort if source_effort is not None else target_current,
                target_efforts,
                getattr(target_backend, "default_reasoning_effort", None),
            )
        else:
            next_effort = target_current if target_current is not None else source_effort
        self.llm_no = index
        self.llmclient = self.llmclients[self.llm_no]
        try: self.llmclient.backend.history = lastc.backend.history
        except Exception:
            raise Exception('[ERROR] BAD session switch: history 无法迁移到新 backend')
        self.llmclient.last_tools = ''
        if next_effort is not None:
            self.llmclient.backend.reasoning_effort = next_effort
        name = self.get_llm_name(model=True)
        if 'glm' in name or 'minimax' in name or 'kimi' in name: load_tool_schema('_cn', include_mcp_tools=False)
        else: load_tool_schema(include_mcp_tools=False)
        return {
            "ok": True,
            "index": self.llm_no,
            "name": self.get_llm_name(),
            "model": self.get_llm_name(model=True),
            "reasoning_effort": next_effort,
        }

    def select_reasoning_effort(self, effort):
        self.load_llm_sessions()
        if getattr(self, "is_running", False):
            return {"ok": False, "code": "busy", "message": "agent is running"}
        descriptor = self.current_llm_descriptor()
        supported = descriptor["reasoningEfforts"]
        if not descriptor["reasoningEffortKnown"]:
            return {
                "ok": False,
                "code": "reasoning_capabilities_unknown",
                "message": "当前模型未声明可选思考等级",
            }
        normalized = str(effort or "").strip().lower()
        if normalized not in supported:
            return {
                "ok": False,
                "code": "unsupported_reasoning_effort",
                "message": f"当前模型不支持 {normalized}；支持：{', '.join(supported)}",
            }
        self.llmclient.backend.reasoning_effort = normalized
        return {"ok": True, "effort": normalized, "model": descriptor["name"]}

    def list_llm_descriptors(self):
        self.load_llm_sessions()
        return [
            self._llm_descriptor(i, client, current=i == self.llm_no)
            for i, client in enumerate(self.llmclients)
        ]

    def list_llms(self):
        self.load_llm_sessions()
        return [(i, self.get_llm_name(b), i == self.llm_no) for i, b in enumerate(self.llmclients)]
    def get_llm_name(self, b=None, model=False):
        b = self.llmclient if b is None else b
        if isinstance(b, dict): return 'BADCONFIG_MIXIN'
        if model: return (getattr(b.backend, 'model', '') or '').lower()
        # 显示成 profile/model（profile 名 = backend.name，来自 llm.yaml）。
        profile = getattr(b.backend, 'name', '') or type(b.backend).__name__
        mdl = getattr(b.backend, 'model', '') or ''
        return f"{profile}/{mdl}" if mdl else profile

    def set_permission_mode(self, mode):
        '''切换主会话权限档：read_only / ask / full_access（默认 full_access）。

        下一轮 put_task 新建 handler 时生效；若当前已有 handler 也同步更新，
        让运行中的会话立刻感知（下一次 dispatch 即按新档评估）。
        '''
        from genericagent.core.permissions.policy import build_permission_mode_policy, normalize_permission_mode
        mode = normalize_permission_mode(mode)
        self.permission_mode = mode
        if self.handler is not None:
            self.handler.permission_mode_policy = build_permission_mode_policy(mode)
        return mode

    def _start_stop_file_watcher(self, interval_s=0.2):
        """Watch `<task_dir>/_stop` on its own thread while a turn is in flight.

        The only other `_stop` check lives in `for chunk in gen`, which never runs during a
        provider call or a tool call: a subagent spawns with verbose=False, and there
        `agent_loop` drives both through `exhaust()`, so nothing is yielded until the whole
        call is done. Measured, that made interrupt latency equal to turn length for an LLM
        call, and made a running tool uninterruptible outright (the second turn still went
        out). A watcher thread is the smallest fix that does not require every long-running
        call site to learn about cancellation.

        Returns a callable that stops the watcher, or None when there is no task dir (the
        interactive frontends abort through `agent.abort()` directly).
        """
        if not self.task_dir:
            return None
        stop_watching = threading.Event()

        def watch():
            while not stop_watching.wait(interval_s):
                try:
                    if consume_file(self.task_dir, '_stop'):
                        self.abort()
                        return
                except Exception:
                    # A transient read failure must not kill the watcher's turn.
                    continue

        thread = threading.Thread(target=watch, daemon=True, name='ga-stop-file-watcher')
        thread.start()

        def cancel():
            stop_watching.set()
            thread.join(timeout=interval_s * 5)

        return cancel

    def abort(self):
        if not self.is_running: return
        print('Abort current task...')
        self.stop_sig = True
        # 解开 ask 档挂起的审批（按 deny 收尾），避免 stop 后工具线程永久 wait
        runtime = getattr(self, 'permission_runtime', None)
        if runtime is not None:
            try:
                runtime.cancel_all()
            except Exception as e:
                print(f"[WARN] permission_runtime.cancel_all failed: {e}")
        gruntime = getattr(self, 'goal_proposal_runtime', None)
        if gruntime is not None:
            try:
                gruntime.cancel_all()
            except Exception as e:
                print(f"[WARN] goal_proposal_runtime.cancel_all failed: {e}")
        for target in (getattr(self, 'llmclient', None), getattr(getattr(self, 'llmclient', None), 'backend', None)):
            cancel = getattr(target, 'cancel_current_request', None)
            if cancel:
                try: cancel()
                except Exception as e: print(f"[WARN] cancel_current_request failed: {e}")
        if self.handler is not None: self.handler.cancel()
            
    def put_task(self, query, source="user", images=None):
        display_queue = queue.Queue()
        self.task_queue.put({"query": query, "source": source, "images": images or [], "output": display_queue})
        return display_queue

    # i know it is dangerous, but raw_query is dangerous enough it doesn't enlarge
    def _expand_prompt(self, expansion_type, command_name, command_args, prompt):
        """Run UserPromptExpansion for a slash-command -> prompt expansion.

        CC fires this when a slash command / MCP prompt is expanded into the
        prompt that reaches the model (m0120:6871). Hooks may add context or
        suppress the original prompt; both are observational-only here.
        """
        text = prompt
        try:
            from genericagent.hooks.runtime import run_hook_event
            agg = run_hook_event(self, 'UserPromptExpansion', {
                'expansion_type': expansion_type,
                'command_name': command_name,
                'command_args': command_args,
                'prompt': prompt,
            }, emit=getattr(self, 'hook_emit', None))
            if agg.context_text:
                text = text + chr(10) + chr(10) + '<hook_context>' + chr(10) + agg.context_text + chr(10) + '</hook_context>'
            suppress = False
            for result in agg.results:
                if result.hook_specific.get('suppressOriginalPrompt') is True:
                    suppress = True
            if suppress:
                return None
        except Exception as e:
            print(f"[WARN] UserPromptExpansion hooks failed: {e}")
        return text

    def _handle_slash_cmd(self, raw_query, display_queue):
        if not raw_query.startswith('/'): return raw_query
        if _sm := re.match(r'/session\.(\w+)=(.*)', raw_query.strip()):
            k, v = _sm.group(1), _sm.group(2)
            vfile = os.path.join(str(paths.TEMP_DIR), v)
            if os.path.isfile(vfile): v = open(vfile, encoding='utf-8').read().strip()
            try: v = json.loads(v)  # cover number parsing
            except (json.JSONDecodeError, ValueError): pass
            setattr(self.llmclient.backend, k, v)
            display_queue.put({'done': smart_format(f"✓ session.{k} = {repr(v)}", max_str_len=500), 'source': 'system'})
            return None
        if _cm := re.match(r'/compact(?:\s+([\s\S]*))?$', raw_query.strip()):
            self._manual_compact(_cm.group(1) or "", display_queue)
            return None
        if raw_query.strip() == '/resume':
            _expanded = r'帮我看看最近有哪些会话可以恢复。读model_responses/目录，按修改时间取最近10个文件，从每个文件里找最后一个<history>...</history>块，用一句话总结每个会话在聊什么，列表给我选。注意读文件后要把字面的\n替换成真换行才能正确匹配。'
            return self._expand_prompt('slash_command', '/resume', '', _expanded)
        if _gm := re.match(r'/goal(?:\s+([\s\S]*))?$', raw_query.strip()):
            self._handle_goal_command(_gm.group(1) or '', display_queue)
            return None
        return raw_query

    def _goal_status_text(self):
        g = getattr(self, 'active_goal', None)
        if g is None:
            return "No goal set."
        turns = f"{g.iterations} turn" + ("s" if g.iterations != 1 else "")
        text = "Goal active: %s (%s)" % (g.condition, turns)
        if g.last_reason:
            text += "\nLast check: %s" % g.last_reason
        return text

    def _goal_token_position(self):
        """Cumulative OUTPUT tokens spent this session (mirrors CC ``Th()``).

        CC's goal card/token accounting uses ``costLedger.totalOutputTokens()``
        (m0017:4936), so ``tokens spent`` = this counter minus the value captured
        when the goal was set. We read ``session_output_tokens`` first; if a
        backend predates that counter we fall back to the latest request's output
        tokens so the figure is never silently zero.
        """
        try:
            backend = getattr(getattr(self, 'llmclient', None), 'backend', None)
            cumulative = getattr(backend, 'session_output_tokens', None)
            if isinstance(cumulative, (int, float)) and cumulative >= 0:
                return int(cumulative)
            usage = getattr(backend, 'last_usage_tokens', None)
            if isinstance(usage, dict):
                out = usage.get('output_tokens')
                if isinstance(out, (int, float)):
                    return int(out)
        except Exception:
            pass
        return 0

    def _notify_goal_metadata(self, *, terminal=None, force=False):
        """Push goal metadata to the UI/SDK when it materially changed (S0).

        Mirrors CC ``notifyMetadataChanged`` (m0696): shallow-compare the small
        field set, and on a terminal transition push a ``terminal`` badge with
        ``goal`` cleared so the card disappears but the badge persists.

        ``force=True`` bypasses the diff so a reconnecting / re-mounting UI can
        get the current snapshot even when nothing changed (S1 robustness). A
        restored terminal badge stashed in ``_pending_terminal_goal`` is flushed
        once on the next push.
        """
        try:
            g = getattr(self, 'active_goal', None)
            current_tokens = self._goal_token_position() if g is not None else None
            meta = goal_metadata.goal_metadata(g, current_tokens=current_tokens)
            pending_terminal = getattr(self, '_pending_terminal_goal', None)
            if g is None and pending_terminal is not None:
                terminal = pending_terminal
            previous = getattr(self, '_last_goal_metadata', None)
            if not force and not goal_metadata.differs(previous, meta) and terminal is None:
                return
            self._last_goal_metadata = meta
            if g is None and pending_terminal is not None:
                self._pending_terminal_goal = None
            emit = getattr(self, 'hook_emit', None)
            if emit is None:
                return
            event = {'type': 'goal_metadata', 'goal': meta}
            if terminal is not None:
                event['terminal'] = terminal
            emit(event)
        except Exception as e:
            print(f"[WARN] Failed to emit goal_metadata: {e}")

    def emit_goal_snapshot(self):
        """Emit the current goal metadata unconditionally (bridge ready/resume)."""
        self._notify_goal_metadata(force=True)

    def _goal_data_root(self):
        return getattr(self, 'data_root', None)

    def _record_goal_status(self, *, met, failed=False, condition, reason="", iterations=0, sentinel=False, duration_ms=None, tokens=None):
        """Best-effort goal_status transcript event (never raises upward)."""
        try:
            path = getattr(self, 'session_path', None)
            if not path:
                return
            session_transcript.record_goal_status(
                path,
                session_id=getattr(self, 'session_id', '') or '',
                met=met, failed=failed, condition=condition, reason=reason,
                iterations=iterations, sentinel=sentinel,
                duration_ms=duration_ms, tokens=tokens,
            )
        except Exception as e:
            print(f"[WARN] Failed to record goal_status: {e}")

    def _record_active_goal(self):
        try:
            path = getattr(self, 'session_path', None)
            if not path:
                return
            session_transcript.record_active_goal(
                path, session_id=getattr(self, 'session_id', '') or '',
                goal=getattr(self, 'active_goal', None),
            )
        except Exception as e:
            print(f"[WARN] Failed to record active_goal: {e}")

    def _supersede_goal(self, old):
        """Record that an existing goal was replaced by a new one (design 9.3).

        Clears the in-memory goal, writes an ``active_goal=None`` snapshot plus a
        sentinel ``goal_status`` (reason=superseded), and emits a ``goal_cleared``
        telemetry row with ``outcome='superseded'`` so an overwrite is never a
        silent mutation.
        """
        try:
            duration_ms = None
            try:
                duration_ms = max(0, int((time.time() - float(old.set_at or time.time())) * 1000))
            except Exception:
                duration_ms = None
            tokens_spent = max(0, self._goal_token_position() - int(old.tokens_at_start or 0))
            self.active_goal = None
            self._record_active_goal()
            self._record_goal_status(
                met=False, condition=old.condition, reason='superseded',
                iterations=old.iterations, sentinel=True,
                duration_ms=duration_ms, tokens=tokens_spent,
            )
            goal_telemetry.record(
                goal_telemetry.EVENT_CLEARED, data_root=self._goal_data_root(),
                origin=getattr(old, 'origin', None), iterations=int(old.iterations),
                durationMs=duration_ms, tokens=int(tokens_spent), outcome='superseded',
            )
        except Exception as e:
            print(f"[WARN] Failed to supersede previous goal: {e}")

    def _set_goal(self, condition, origin=goal_core.ORIGIN_USER):
        previous = getattr(self, 'active_goal', None)
        if previous is not None:
            self._supersede_goal(previous)
        tokens_at_start = self._goal_token_position()
        self.active_goal = goal_core.ActiveGoal(
            condition=condition, origin=origin, tokens_at_start=tokens_at_start,
        )
        # Register the Stop evaluator hook (idempotent on key="goal:stop").
        try:
            goal_evaluator.register_goal_hook(self, condition)
        except Exception as e:
            print(f"[WARN] Failed to register goal hook: {e}")
        self._record_active_goal()
        self._record_goal_status(met=False, condition=condition, iterations=0, sentinel=True)
        self._notify_goal_metadata()
        goal_telemetry.record(
            goal_telemetry.EVENT_SET, data_root=self._goal_data_root(), origin=origin,
        )
        return self.active_goal

    def _clear_goal(self, *, met, failed=False, reason="", iterations=None, outcome=None):
        g = getattr(self, 'active_goal', None)
        if g is None:
            return None
        iters = g.iterations if iterations is None else int(iterations)
        duration_ms = None
        try:
            duration_ms = max(0, int((time.time() - float(g.set_at or time.time())) * 1000))
        except Exception:
            duration_ms = None
        tokens_spent = max(0, self._goal_token_position() - int(g.tokens_at_start or 0))
        self.active_goal = None
        try:
            goal_evaluator.unregister_goal_hook(self)
        except Exception as e:
            print(f"[WARN] Failed to unregister goal hook: {e}")
        self._record_active_goal()
        self._record_goal_status(
            met=met, failed=failed, condition=g.condition, reason=reason,
            iterations=iters, duration_ms=duration_ms, tokens=tokens_spent,
        )
        terminal = None
        if met or failed:
            terminal = {
                'met': bool(met), 'failed': bool(failed), 'condition': g.condition,
                'iterations': int(iters), 'tokens': int(tokens_spent),
            }
            if duration_ms is not None:
                terminal['durationMs'] = int(duration_ms)
        self._notify_goal_metadata(terminal=terminal)
        event = goal_telemetry.EVENT_ACHIEVED if met else (
            goal_telemetry.EVENT_FAILED if failed else goal_telemetry.EVENT_CLEARED)
        if outcome is None:
            outcome = 'met' if met else ('failed' if failed else 'user_cleared')
        goal_telemetry.record(
            event, data_root=self._goal_data_root(), origin=getattr(g, 'origin', None),
            iterations=int(iters), durationMs=duration_ms, tokens=int(tokens_spent),
            outcome=outcome,
        )
        return g

    def _record_goal_deferred(self):
        """Record a deferred goal evaluation (background sub-agents in flight).

        Stage 2-A: the goal is still active but this turn's evaluation was
        skipped so the in-flight background work can land. Not met, not failed.
        """
        g = getattr(self, 'active_goal', None)
        if g is None:
            return
        try:
            self._record_goal_status(
                met=False, condition=g.condition, reason='deferred',
                iterations=g.iterations,
            )
        except Exception as e:
            print(f"[WARN] Failed to record deferred goal_status: {e}")

    def _goal_proposal_gate(self):
        """Why a model-proposed goal is rejected right now, or None if allowed.

        Mirrors CC ProposeGoal ``call()`` gate order (m1718:205-283). Only the
        main session may propose; subagents, plan mode, the feature flag and the
        ``model_proposed_goals=disabled`` setting all block it.
        """
        if getattr(self, 'is_subagent', False):
            return "propose_goal is only available in the main session, not in a subagent"
        if not goal_proposal.is_feature_enabled():
            return "proposed goals are disabled (GA_PROPOSE_GOAL_ENABLED is off)"
        handler = getattr(self, 'handler', None)
        in_plan = False
        try:
            in_plan = bool(handler is not None and handler._in_plan_mode())
        except Exception:
            in_plan = False
        if in_plan:
            return "cannot propose a goal while in plan mode; finish the plan first"
        setting = goal_proposal.read_model_proposed_goals_setting()
        if setting == goal_proposal.SETTING_DISABLED:
            return "model-proposed goals are disabled by the model_proposed_goals setting"
        return None

    def _apply_goal_proposal(self, condition, *, ask_user=True, origin=None):
        """Apply a validated model proposal. Returns a short user-facing status.

        Direct path (no approval): record the origin then enqueue ``/goal`` on the
        task queue, exactly like a user typing it (CC defers the same way). Ask
        path: show the one-keypress dialog and, on approve, queue the same
        command. Non-blocking either way.
        """
        if ask_user:
            runtime = getattr(self, 'goal_proposal_runtime', None)
            if runtime is None or not runtime.has_emit():
                return "goal proposal could not be shown (no approval UI available)"
            approved = runtime.request_approval(condition)
            if not approved:
                # CC: a rejected proposal never notifies the model and must not be
                # asked about or re-proposed.
                return "goal proposal was not approved; do not ask about it again or re-propose it"
            if origin is None:
                origin = goal_core.ORIGIN_PROPOSAL_APPROVED
        else:
            if origin is None:
                origin = goal_core.ORIGIN_PROPOSAL_DIRECT
        self._queued_goal_origin = origin
        try:
            self.put_task('/goal ' + condition, source='goal_proposal')
        except Exception as e:
            self._queued_goal_origin = None
            return f"failed to queue goal: {e}"
        goal_telemetry.record(
            goal_telemetry.EVENT_PROPOSED, data_root=self._goal_data_root(),
            origin=origin, ask_user=bool(ask_user), reason_len=len(str(condition or "")),
        )
        goal_telemetry.record(
            goal_telemetry.EVENT_PROPOSAL_DECIDED, data_root=self._goal_data_root(),
            origin=origin, ask_user=bool(ask_user), outcome='approved' if ask_user else 'direct',
        )
        if ask_user:
            return "goal approved; it takes effect when this turn ends. Keep working; do not wait for a kickoff."
        return "goal set directly; it takes effect when this turn ends. Keep working; do not wait for a kickoff."

    def _apply_goal_verdict(self, status):
        """Apply an interpreted goal verdict from the Stop gate.

        - met / failed: clear the goal + hook, write a terminal goal_status.
        - not_met (ok:false): bump iterations + last_reason, keep goal/hook.
        - evaluator_error / timeout: do not treat as met; leave goal active and
          let the turn end (the Stop gate already fails open for those).
        """
        g = getattr(self, 'active_goal', None)
        if g is None or status is None:
            return
        if status.error_kind:
            # Evaluator failed (error/timeout/invalid_output). Never treat as
            # met; keep the goal active and let the turn end (the Stop gate
            # already fails open). Record it so the failure is not silent.
            print(f"[WARN] goal evaluator {status.error_kind}; goal stays active")
            self._record_goal_status(
                met=False, condition=g.condition,
                reason=status.error_kind, iterations=g.iterations,
            )
            goal_telemetry.record(
                goal_telemetry.EVENT_MET, data_root=self._goal_data_root(),
                outcome=status.error_kind, met=False, iterations=int(g.iterations),
            )
            return
        if status.met:
            goal_telemetry.record(
                goal_telemetry.EVENT_MET, data_root=self._goal_data_root(),
                outcome='met', met=True, iterations=int(g.iterations + 1),
            )
            self._clear_goal(met=True, reason=status.reason, iterations=g.iterations + 1)
            return
        if status.failed:
            outcome = 'impossible' if getattr(status, 'impossible', False) else 'failed'
            goal_telemetry.record(
                goal_telemetry.EVENT_MET, data_root=self._goal_data_root(),
                outcome=outcome, met=False, iterations=int(g.iterations + 1),
            )
            self._clear_goal(met=False, failed=True, reason=status.reason, iterations=g.iterations + 1)
            return
        # not met: keep working toward it
        self.active_goal = g.bumped(status.reason)
        self._record_active_goal()
        self._record_goal_status(
            met=False, condition=g.condition, reason=status.reason,
            iterations=self.active_goal.iterations,
        )
        self._notify_goal_metadata()

    def _handle_goal_command(self, arg, display_queue):
        cmd = goal_core.parse_goal_command(arg)
        if cmd.kind == 'error':
            display_queue.put({'done': cmd.error, 'source': 'system'})
            return
        if cmd.kind == 'status':
            display_queue.put({'done': self._goal_status_text(), 'source': 'system'})
            return
        if cmd.kind == 'clear':
            cleared = self._clear_goal(met=False, reason='cleared by user', outcome='user_cleared')
            if cleared is None:
                display_queue.put({'done': 'No goal set.', 'source': 'system'})
            else:
                display_queue.put({'done': f'Goal cleared: {cleared.condition}', 'source': 'system'})
            return
        # set
        origin = getattr(self, '_queued_goal_origin', None) or goal_core.ORIGIN_USER
        self._queued_goal_origin = None
        g = self._set_goal(cmd.condition, origin=origin)
        # Kickoff: nudge the model to keep working until the goal is met, on the
        # next real model input. Skipped when this was called headless in tests.
        try:
            self.history.append(
                '[SYSTEM] A session goal is now active with condition: "%s". '
                'Continue working toward the condition; the evaluator will block '
                'turn-end until it holds. It auto-clears once met (do not ask the '
                'user to clear after success).' % g.condition
            )
        except Exception:
            pass
        display_queue.put({'done': f'Goal set: {g.condition}', 'source': 'system'})

    def _manual_compact(self, instructions, display_queue):
        """手动 /compact：用 LLM 摘要替换长历史。CLI/TUI 复用与 ink 相同的核心。"""
        if compact_agent_context is None:
            display_queue.put({'done': "/compact 不可用（compact_context 未加载）", 'source': 'system'})
            return
        from genericagent.hooks.runtime import run_hook_event as _run_compact_hook
        try:
            pre = _run_compact_hook(self, 'PreCompact', {
                'trigger': 'manual',
                'custom_instructions': str(instructions or ''),
            }, emit=getattr(self, 'hook_emit', None))
            if pre.blocked:
                display_queue.put({'done': pre.reason or 'PreCompact hook blocked compaction', 'source': 'system'})
                return
        except Exception as e:
            print(f"[WARN] PreCompact hooks failed: {e}")
        result = compact_agent_context(self, instructions=str(instructions or ""))
        if not result.ok:
            display_queue.put({'done': f"压缩失败：{result.message}", 'source': 'system'})
            return
        try:
            post = _run_compact_hook(self, 'PostCompact', {
                'trigger': 'manual',
                'compact_summary': result.message,
            }, emit=getattr(self, 'hook_emit', None))
            if post.context_text:
                self.history.append(f"[HOOK CONTEXT]: {post.context_text}")
        except Exception as e:
            print(f"[WARN] PostCompact hooks failed: {e}")
        # 同步刷新日志与会话记录，与 ink_bridge 一致。
        try:
            if replace_log_with_compact_history is not None:
                replace_log_with_compact_history(getattr(self, "log_path", None), copy.deepcopy(self.llmclient.backend.history))
        except Exception: pass
        try:
            if session_transcript is not None and getattr(self, "session_path", None):
                if session_transcript._detect_session_version(self.session_path) == 2:
                    session_transcript.record_compact_v2(
                        self.session_path,
                        session_id=getattr(self, "session_id", ""),
                        summary=result.summary or result.message,
                        trigger="manual",
                        messages_summarized=result.original_messages,
                        turn_id=getattr(self, "session_turn_id", None),
                        working_state=getattr(result, "working_state", None),
                    )
                else:
                    session_transcript.record_compact(
                        self.session_path, session_id=getattr(self, "session_id", ""),
                        message=result.message, backend_history_after=copy.deepcopy(self.llmclient.backend.history))
        except Exception: pass
        display_queue.put({'done': smart_format(result.message, max_str_len=500), 'source': 'system'})

    def _emit_message_display(self, handler, final_text):
        """Run MessageDisplay for the turn's final assistant message.

        Display-only (CC m0120:7053): a hook may return displayContent to alter
        what is shown; the stored transcript is unaffected (it is recorded from
        the committed model text separately).
        """
        shown = final_text
        try:
            from genericagent.hooks.runtime import run_hook_event
            turn_id = getattr(handler, 'current_turn', 0)
            agg = run_hook_event(self, 'MessageDisplay', {
                'turn_id': turn_id,
                'message_id': getattr(self, 'session_turn_id', 0),
                'index': 0,
                'final': True,
                'delta': final_text,
            }, emit=getattr(self, 'hook_emit', None))
            for result in agg.results:
                content = result.hook_specific.get('displayContent')
                if isinstance(content, str):
                    shown = content
        except Exception as e:
            print(f"[WARN] MessageDisplay hooks failed: {e}")
        return shown

    def run(self):
        while True:
            task = self.task_queue.get()
            raw_query, source, images, display_queue = task["query"], task["source"], task.get("images") or [], task["output"]
            raw_query = self._handle_slash_cmd(raw_query, display_queue)
            if raw_query is None:
                self.task_queue.task_done(); continue
            # UserPromptSubmit is a user-turn boundary, not an internal LLM turn.
            # It runs after slash-command handling so commands such as /model do
            # not masquerade as user prompts.
            from genericagent.hooks.runtime import hook_runtime_for_agent, run_hook_event
            self.hook_runtime = hook_runtime_for_agent(self)
            prompt_hooks = run_hook_event(
                self,
                'UserPromptSubmit',
                {'prompt': raw_query, 'source': source},
                emit=getattr(self, 'hook_emit', None),
            )
            if prompt_hooks.updated_input and isinstance(prompt_hooks.updated_input.get('prompt'), str):
                raw_query = prompt_hooks.updated_input['prompt']
            if prompt_hooks.blocked:
                reason = prompt_hooks.reason or 'request blocked by UserPromptSubmit hook'
                display_queue.put({'done': f'Hook blocked the request: {reason}', 'source': 'system'})
                self.task_queue.task_done()
                continue
            if prompt_hooks.context_text:
                raw_query = (
                    raw_query
                    + "\n\n<hook_context>\n"
                    + prompt_hooks.context_text
                    + "\n</hook_context>"
                )
            if prompt_hooks.system_text:
                print(f"[Hook] {prompt_hooks.system_text}")
            self.is_running = True
            reset_cancel = getattr(self.llmclient, 'reset_cancel', None)
            if reset_cancel:
                try: reset_cancel()
                except Exception as e: print(f"[WARN] reset_cancel failed: {e}")
            rquery = smart_format(raw_query.replace('\n', ' '), max_str_len=200)
            self.history.append(f"[USER]: {rquery}")
            
            sections = build_system_prompt_sections(self)
            changed_sections = note_system_prompt_sections(self, sections)
            if changed_sections:
                print(f"[Prompt] system prompt sections changed: {', '.join(changed_sections)}")
            sys_prompt = render_system_prompt(sections) + getattr(self.llmclient.backend, 'extra_sys_prompt', '')
            if self.peer_hint: sys_prompt += f"\n[Peer] 用户提及其他会话/后台任务状态时: temp/model_responses/ (只找近期修改的文件尾部)\n"
            project_root = str(getattr(self, "project_root", None) or paths.PROJECT_ROOT)
            tool_cwd = str(getattr(self, "tool_cwd", None) or project_root)
            handler = GenericAgentHandler(self, self.history, tool_cwd, workspace_root=project_root)
            handler.hook_runtime = self.hook_runtime
            resumed_state = getattr(self, "_resumed_working_state", None)
            if isinstance(resumed_state, dict):
                restored_working = resumed_state.get("handler_working")
                if isinstance(restored_working, dict):
                    handler.working.update(copy.deepcopy(restored_working))
                self._resumed_working_state = None
            if self.handler and 'key_info' in self.handler.working: 
                ki = re.sub(r'\n\[SYSTEM\] 此为.*?工作记忆[。\n]*', '', self.handler.working['key_info'])  # 去旧
                handler.working['key_info'] = ki
                handler.working['passed_sessions'] = ps = self.handler.working.get('passed_sessions', 0) + 1
                if ps > 0: handler.working['key_info'] += f'\n[SYSTEM] 此为 {ps} 个对话前设置的key_info，若已在新任务，先更新或清除工作记忆。\n'
            # 三档权限（read_only/ask/full_access，默认 full_access）：给主会话 handler
            # 挂上 mode policy，让 dispatch 在工具执行前按档位放行/拒绝/请求审批。
            # workflow 子 agent 有独立的 workflow_permission_policy，优先级更高，互不影响。
            try:
                from genericagent.core.permissions.policy import build_permission_mode_policy
                handler.permission_mode_policy = build_permission_mode_policy(
                    getattr(self, 'permission_mode', None)
                )
            except Exception as e:
                print(f"[WARN] Failed to attach permission mode policy: {e}")
            # 进程型 subagent 专用权限隔离；workflow policy 仍由 workflow_child_agent 自己管理。
            handler.subagent_permission_policy = getattr(self, 'subagent_permission_policy', None)
            subagent_tool_before = getattr(self, '_subagent_transcript_tool_before', None)
            if subagent_tool_before is not None:
                handler.tool_before_callback = subagent_tool_before
            subagent_tool_after = getattr(self, '_subagent_transcript_tool_after', None)
            if subagent_tool_after is not None:
                handler.tool_after_callback = subagent_tool_after
            subagent_permission_callback = getattr(self, '_subagent_transcript_permission', None)
            if subagent_permission_callback is not None:
                handler.subagent_permission_event_callback = subagent_permission_callback
            # 共享 agent 级 runtime（含 bridge emit）；每轮 handler 都挂上同一实例
            handler.permission_runtime = getattr(self, 'permission_runtime', None)
            self.handler = handler  # although new handler, the **full** history is in llmclient, so it is full history!
            self.llmclient.log_path = self.log_path
            transcript_history_before = transcript_delta_anchor(self)
            can_image = supports_image_input(self.llmclient)
            initial_content = _build_user_content_with_images(raw_query, images) if can_image else None
            if (images or _extract_image_paths(raw_query)) and not can_image:
                # 门控关闭：不把 image block 发给 API（避免 400「不支持 image」）。
                # 附件 path 若不在文本里（常见 [Image #N] 占位），补进 query 供工具/OCR，并给用户友好提示。
                path_hints = []
                for item in image_codec.normalize_image_attachments(images):
                    p = item.get("path") or ""
                    if p and os.path.isfile(p) and p not in path_hints:
                        path_hints.append(p)
                for p in _extract_image_paths(raw_query):
                    if p not in path_hints:
                        path_hints.append(p)
                model_label = ""
                try:
                    model_label = self.get_llm_name(model=True) or ""
                except Exception:
                    model_label = ""
                model_bit = f"「{model_label}」" if model_label else "当前模型"
                try:
                    if path_hints:
                        display_queue.put(
                            f"[image] {model_bit}不支持原生识图，图片未作为多模态内容发送。"
                            f"已把本地路径写入文本，可用 OCR/工具读文件；或 /model 切换到支持 image_input 的模型。\n"
                            f"  paths: {'; '.join(path_hints)}\n"
                        )
                    else:
                        display_queue.put(
                            f"[image] {model_bit}不支持原生识图，图片未发送。"
                            f"请 /model 切换到支持 image_input 的模型，或用文字描述图片内容。\n"
                        )
                except Exception:
                    pass
                if path_hints:
                    # 让模型看到真实路径，避免只剩 [Image #N] 占位却去瞎找文件
                    path_block = "\n".join(f"- {p}" for p in path_hints)
                    raw_query = (
                        f"{raw_query}\n\n"
                        f"[系统] {model_bit}无原生图片输入能力，上述 [Image #N] 仅是占位符，你看不到像素。"
                        f"用户附件本地路径如下（可用 OCR/file_read 等工具，禁止假装已看见图内容）：\n"
                        f"{path_block}\n"
                    )
            # 原生图在「本轮 content」或「近期 history」（如用户只说重试）时，强制 vision 直答。
            # 对齐 Codex：图已在 thread 则不要再工具加载；对齐 CC：粘贴图直接进多模态 user content。
            _inject, _via, _n_img = should_inject_vision_direct_answer(
                can_image=can_image,
                initial_content=initial_content,
                history=backend_history(self.llmclient),
                user_text=raw_query,
            )
            if _inject:
                sys_prompt += build_vision_direct_answer_sys_prompt(image_count=_n_img, via=_via)
            name = self.get_llm_name(model=True)
            from genericagent.mcp.runtime import mcp_cancellation_scope
            with mcp_cancellation_scope(handler.code_stop_signal):
                load_tool_schema('_cn' if ('glm' in name or 'minimax' in name or 'kimi' in name) else '', include_mcp_tools=True)
            from genericagent.agents.capabilities import (
                build_subagent_capability_profile,
                filter_tool_schema_for_capabilities,
            )
            available_tool_names = [
                item.get('function', {}).get('name')
                for item in TOOLS_SCHEMA
                if isinstance(item, dict)
            ]
            # Rebuild from the current schema every turn: MCP discovery may append
            # tools after the child process has already entered its first turn.  A
            # deliberately empty schema is also used by lightweight test/fake
            # backends; leave dispatch to the existing permission policy there.
            if not available_tool_names and getattr(self, 'capability_role_tools', None) is None:
                profile = None
            else:
                profile = build_subagent_capability_profile(
                    is_subagent=bool(getattr(self, 'is_subagent', False)),
                    role_tools=getattr(self, 'capability_role_tools', None),
                    allow_delegation=bool(getattr(self, 'capability_allow_delegation', False)),
                    team_mode=bool(getattr(self, 'capability_team_mode', False)),
                    available_tools=available_tool_names,
                )
            self.capability_profile = profile
            handler.capability_profile = profile
            effective_tools_schema = (
                TOOLS_SCHEMA
                if profile is None
                else filter_tool_schema_for_capabilities(TOOLS_SCHEMA, profile)
            )
            gen = agent_runner_loop(self.llmclient, sys_prompt, raw_query,
                                handler, effective_tools_schema, max_turns=70, verbose=self.verbose,
                                initial_user_content=initial_content)
            stop_watcher = self._start_stop_file_watcher()
            try:
                full_resp = ""; last_pos = 0
                for chunk in gen:
                    if consume_file(self.task_dir, '_stop'): self.abort()
                    if self.stop_sig: break
                    full_resp += chunk
                    if should_flush_display_delta(full_resp, last_pos, chunk):
                        display_queue.put({'next': full_resp[last_pos:] if self.inc_out else full_resp, 'source': source})
                        last_pos = len(full_resp)
                if self.inc_out and last_pos < len(full_resp): display_queue.put({'next': full_resp[last_pos:], 'source': source})
                # Do NOT rewrite markup only on `done` (e.g. </summary> → </summary>\n\n).
                # Streamed `next` chunks already shipped the raw text; a done-only rewrite
                # makes the full payload diverge from the stream-commit prefix and the
                # Ink UI re-paints Turn 1 + tools a second time. Display spacing belongs
                # in the frontend formatter (formatAssistantText).
                _display_text = self._emit_message_display(handler, full_resp)
                display_queue.put({'done': _display_text, 'source': source})
                # full_resp is the UI stream (turn banner + tool progress +
                # final prose). CC's Stop payload is the last assistant message
                # text, so persist/send the committed model text separately
                # while leaving Ink's stream behavior untouched.
                final_assistant_text = (
                    getattr(handler, 'last_assistant_message', '') or full_resp
                )
                subagent_assistant_callback = getattr(self, '_subagent_transcript_assistant', None)
                if subagent_assistant_callback is not None:
                    try: subagent_assistant_callback(final_assistant_text)
                    except Exception: pass
                self.history = handler.history_info
                try:
                    session_transcript.record_agent_turn(
                        self,
                        user_text=raw_query,
                        assistant_text=final_assistant_text,
                        source=source,
                        backend_history_before=transcript_history_before,
                    )
                except Exception as e:
                    print(f"[WARN] Failed to record session transcript: {e}")
                if not getattr(self, 'is_subagent', False):
                    try:
                        # The Stop gate inside agent_runner_loop already emitted
                        # Stop when the model wanted to stop. Emit here only as a
                        # fallback for turns the gate never reached (no hook
                        # runtime, explicit exit, error, or user abort mid-loop),
                        # so a beep still fires exactly once and never doubles.
                        _stop_already_emitted = bool(getattr(handler, 'stop_emitted', False))
                        if not _stop_already_emitted:
                            run_hook_event(
                                self, 'Stop',
                                {
                                    'stop_hook_active': False,
                                    'last_assistant_message': final_assistant_text,
                                    'exit_reason': 'aborted' if self.stop_sig else 'completed',
                                    'source': source,
                                    'phase': 'user_turn_end',
                                },
                                emit=getattr(self, 'hook_emit', None),
                            )
                    except Exception as hook_err:
                        print(f"[WARN] Stop hooks failed: {hook_err}")
            except Exception as e:
                print(f"Backend Error: {format_error(e)}")
                failed_resp = full_resp + f'\n```\n{format_error(e)}\n```'
                display_queue.put({'done': failed_resp, 'source': source})
                final_assistant_text = (
                    getattr(handler, 'last_assistant_message', '') or failed_resp
                )
                subagent_assistant_callback = getattr(self, '_subagent_transcript_assistant', None)
                if subagent_assistant_callback is not None:
                    try: subagent_assistant_callback(final_assistant_text, error=format_error(e))
                    except Exception: pass
                try:
                    session_transcript.record_agent_turn(
                        self,
                        user_text=raw_query,
                        assistant_text=final_assistant_text,
                        source=source,
                        backend_history_before=transcript_history_before,
                    )
                except Exception as rec_e:
                    print(f"[WARN] Failed to record session transcript: {rec_e}")
                if not getattr(self, 'is_subagent', False):
                    try:
                        run_hook_event(
                            self, 'StopFailure',
                            {
                                'last_assistant_message': final_assistant_text,
                                'exit_reason': 'failed',
                                'error': format_error(e),
                                'source': source,
                            },
                            emit=getattr(self, 'hook_emit', None),
                        )
                    except Exception as hook_err:
                        print(f"[WARN] StopFailure hooks failed: {hook_err}")
            finally:
                if stop_watcher is not None: stop_watcher()
                if self.stop_sig: print('User aborted the task.')
                self.is_running = self.stop_sig = False
                self.task_queue.task_done()
                if self.handler is not None: self.handler.cancel()

GeneraticAgent = GenericAgent    

def _subagent_state(task_dir, task_name, nround, turn_status, process_status, output_path=None, final_output_path=None, final_output_sha256=None, last_error=None, last_error_stage=None):
    state_path = os.path.join(task_dir, 'state.json')
    old = {}
    try:
        with open(state_path, encoding='utf-8', errors='replace') as f:
            old = json.load(f)
    except FileNotFoundError:
        pass
    except Exception:
        # Unparsable bytes are quarantined once at worker-loop entry
        # (_quarantine_unreadable_state); here we just fall back to a fresh dict so the turn
        # keeps going.
        pass
    old.update({
        'schema_version': 1,
        'task_name': task_name,
        'agent_path': old.get('agent_path') or f'/root/{task_name}',
        'pid': os.getpid(),
        'round': int(nround) if isinstance(nround, int) else 0,
        'turn_status': turn_status,
        'process_status': process_status,
        'updated_at': now_iso(),
        'input_path': os.path.join(task_dir, 'input.txt'),
        'output_path': str(output_path) if output_path else old.get('output_path'),
        'final_output_path': str(final_output_path) if final_output_path else old.get('final_output_path'),
        'final_output_sha256': final_output_sha256 if final_output_sha256 else old.get('final_output_sha256'),
        'last_error': last_error,
        'last_error_stage': last_error_stage if last_error_stage else old.get('last_error_stage'),
        'completion_status': (
            'completed' if turn_status == 'completed' else
            'failed' if turn_status == 'errored' else
            'interrupted' if turn_status == 'interrupted' else
            old.get('completion_status') or 'pending'
        ),
    })
    if turn_status == 'running':
        old['last_round_started_at'] = old['updated_at']
    if turn_status == 'completed':
        old['last_round_end_at'] = old['updated_at']
    atomic_write_json(state_path, old)
    return old

PARENT_INBOX_EVENT_TYPES = {
    'agent_started',
    'turn_completed',
    'agent_waiting_reply',
    'agent_exited',
    'agent_shutdown',
    'agent_error',
    'agent_closed',
}

def _quarantine_unreadable_state(task_dir, task_name):
    """If `state.json` exists but will not parse, copy it aside and record why.

    Called once at worker-loop entry rather than inside `_subagent_state`, because by the time
    any writer runs the file has already been re-read as `{}` and the next atomic write drops
    the original for good.
    """
    state_path = os.path.join(task_dir, 'state.json')
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, encoding='utf-8', errors='replace') as f:
            json.load(f)
        return None
    except Exception as e:
        return _quarantine_corrupt_file(task_dir, task_name, state_path, 'state_load_failed', '', e)


def _quarantine_corrupt_file(task_dir, task_name, path, event_type, nround, error):
    """Copy an unparsable control file aside and say so in the event stream.

    Returns the quarantine path, or None if the copy itself failed. Deliberately best-effort:
    it exists so a corrupt file leaves evidence instead of vanishing, and must never be the
    reason a turn fails.
    """
    quarantine = f'{path}.malformed.{int(time.time() * 1000)}'
    try:
        with open(path, encoding='utf-8', errors='replace') as src:
            raw = src.read()
        with open(quarantine, 'w', encoding='utf-8', newline='\n') as dst:
            dst.write(raw)
    except OSError:
        quarantine = None
    _subagent_event(task_dir, {
        'type': event_type,
        'task_name': task_name,
        'round': int(nround) if isinstance(nround, int) else 0,
        'path': str(path),
        'quarantine_path': quarantine,
        'error': format_error(error),
    })
    return quarantine


def _load_forked_history(agent, task_dir, task_name, raw_history, nround):
    """Apply a forked/resumed backend history, surviving a corrupt file.

    Measured: a truncated `_history.json` raised a bare `JSONDecodeError` out of
    `run_task_worker_loop`, so the child died before writing `state.json`'s turn_status,
    before any event, and after `consume_file` had already deleted the only copy of the bad
    bytes. A corrupt resume projection costs the forked context, not the task — the turn
    still runs on whatever history the client already has, and the file is set aside so the
    parent can see what it actually handed over.
    """
    try:
        agent.llmclient.backend.history = json.loads(raw_history)
        return True
    except Exception as e:
        quarantine = os.path.join(task_dir, f'_history.json.malformed.{int(time.time() * 1000)}')
        try:
            with open(quarantine, 'w', encoding='utf-8', newline='\n') as f:
                f.write(raw_history)
        except OSError:
            quarantine = None
        _subagent_event(task_dir, {
            'type': 'history_load_failed',
            'task_name': task_name,
            'round': int(nround) if isinstance(nround, int) else 0,
            'path': os.path.join(task_dir, '_history.json'),
            'quarantine_path': quarantine,
            'error': format_error(e),
        })
        return False


def _subagent_transcript_append(task_dir, event_type, payload):
    try:
        state = read_json_or_none(os.path.join(task_dir, 'state.json')) or {}
        parent_session_id = state.get('parent_session_id') or payload.get('parent_session_id')
        run_id = state.get('run_id') or payload.get('run_id')
        if parent_session_id and run_id:
            from genericagent.agents.transcript import SubagentTranscriptStore
            SubagentTranscriptStore(os.path.join(os.path.dirname(task_dir), 'sessions')).append_event(
                parent_session_id,
                run_id,
                event_type,
                payload,
            )
    except Exception:
        pass


_REALTIME_SUBSCRIBER = None


def _set_realtime_subscriber(subscriber):
    """One process is one agent, so the subscriber is process-wide state.

    _subagent_event is called from eleven places and is the single choke point where this
    process appends something durable — which is exactly when the parent should be woken.
    Threading the subscriber through every call site would add a parameter to all of them to
    say the same thing this module-level handle says once.
    """
    global _REALTIME_SUBSCRIBER
    _REALTIME_SUBSCRIBER = subscriber
    return subscriber


def _signal_parent():
    subscriber = _REALTIME_SUBSCRIBER
    if subscriber is None:
        return False
    try:
        return bool(subscriber.signal())
    except Exception:
        return False


def _release_subagent_slot(task_dir):
    """Hand the registry slot back the moment this child finishes its work.

    Mirrors Codex's explicit ``release_spawned_thread``: the value of an identity-keyed release
    is that it does not depend on observing the pid, which Windows recycles. The registry
    re-checks the caller's identity, so a stale child cannot close its own successor's row.
    """
    try:
        state = read_json_or_none(os.path.join(task_dir, 'state.json')) or {}
        agent_path = state.get('agent_path')
        if not agent_path:
            return None
        from genericagent.agents.registry import SubagentRegistry, _default_process_identity

        pid = os.getpid()
        try:
            pid_create_time = _default_process_identity(pid)
        except Exception:
            pid_create_time = None
        registry = SubagentRegistry(os.path.join(os.path.dirname(task_dir), 'subagents'))
        return registry.release_agent_slot(agent_path, pid=pid, pid_create_time=pid_create_time)
    except Exception:
        return None


def _subagent_event(task_dir, event):
    append_jsonl_event(os.path.join(task_dir, 'events.jsonl'), event)
    is_turn_started = event.get('type') == 'turn_started'
    if is_turn_started:
        _record_startup_phase('local_turn_event_written', task_dir)
    task_name = event.get('task_name') or os.path.basename(os.path.normpath(task_dir))
    state = read_json_or_none(os.path.join(task_dir, 'state.json')) or {}
    agent_path = event.get('agent_path') or state.get('agent_path') or f'/root/{task_name}'
    run_id = state.get('run_id') or event.get('run_id')
    payload = {k: v for k, v in event.items() if k not in {'type', 'task_name', 'agent_path', 'run_id'}}
    status = {
        'turn_status': state.get('turn_status'),
        'process_status': state.get('process_status'),
    }
    bus_status = 'ok'
    bus_error = None
    try:
        from genericagent.agents.event_bus import SubagentEventBus
        SubagentEventBus(os.path.join(os.path.dirname(task_dir), 'subagents')).append_event(
            event.get('type') or 'subagent_update',
            agent_path=agent_path,
            run_id=run_id,
            task_name=task_name,
            status=status,
            payload=payload,
            notify=event.get('type') in {'turn_completed', 'agent_exited', 'agent_shutdown', 'agent_error', 'agent_closed'},
        )
    except Exception as e:
        bus_status = 'error'
        bus_error = format_error(e)
    if is_turn_started:
        _record_startup_phase('event_bus_mirror_complete', task_dir, status=bus_status, error=bus_error)
    try:
        parent_session_id = state.get('parent_session_id') or event.get('parent_session_id')
        if parent_session_id and run_id:
            _subagent_transcript_append(
                task_dir,
                event.get('type') or 'subagent_update',
                {
                    'task_name': task_name,
                    'agent_path': agent_path,
                    'status': status,
                    **payload,
                },
            )
    except Exception:
        pass
    if event.get('type') in PARENT_INBOX_EVENT_TYPES:
        append_parent_inbox_event(task_dir, event)
    # Durable write is done; now poke the parent so its wait doesn't sit out the poll interval.
    _signal_parent()

def _record_child_ipc_status(task_dir, task_name, status, fallback_reason, address=None):
    state_path = os.path.join(task_dir, 'state.json')
    state = read_json_or_none(state_path) or {}
    state['child_ipc_status'] = status
    state['child_ipc_fallback_reason'] = fallback_reason
    state['child_ipc_address'] = address
    atomic_write_json(state_path, state)
    # 'file' is the default transport, so an event there would add a line to every
    # subagent's stream while saying nothing. Only realtime outcomes are worth an event.
    if status != 'file':
        _subagent_event(task_dir, {
            'type': 'child_ipc_status',
            'task_name': task_name,
            'child_ipc_status': status,
            'child_ipc_fallback_reason': fallback_reason,
        })
    return state


def resolve_reply_wait_schedule_from_env(env=None):
    """Read the poll/lifetime knobs from the environment.

    Children are launched by SubagentManager with a fixed command line that carries no
    timing, so the env is the only path that reaches a real spawned subagent. Unset or
    unparseable values fall through to the defaults rather than failing the launch.
    """
    env = os.environ if env is None else env
    overrides = {}
    for key, name in (('GA_SUBAGENT_POLL_INTERVAL_S', 'poll_interval_s'), ('GA_SUBAGENT_IDLE_TIMEOUT_S', 'idle_timeout_s')):
        raw = env.get(key)
        if raw is None or not str(raw).strip():
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        overrides[name] = value
    return overrides


def _reply_wait_slices(interval, timeout, max_iterations, monotonic_fn):
    """Yield each wait duration for one reply-wait window.

    Deadline-driven so the poll interval can change without changing the lifetime; the last
    slice is clamped to the deadline so a coarse interval cannot overshoot it.
    """
    if max_iterations is not None:
        for _ in range(max_iterations):
            yield interval
        return
    deadline = monotonic_fn() + timeout
    while True:
        remaining = deadline - monotonic_fn()
        # Epsilon guard: float accumulation of many small slices can land a hair short of
        # the deadline and buy a spurious extra round.
        if remaining <= 1e-9:
            return
        yield min(interval, remaining)


def resolve_reply_wait_schedule(reply_wait_iterations, reply_sleep_s, poll_interval_s, idle_timeout_s):
    """Separate wake-up granularity from total idle lifetime.

    These used to be one knob: reply_wait_iterations x reply_sleep_s set both, so halving
    the latency also halved how long an idle subagent stayed alive. Now poll_interval_s
    controls granularity and idle_timeout_s controls lifetime. The legacy iteration params
    are still honoured verbatim so existing callers and tests keep their exact timing.
    """
    interval = float(poll_interval_s) if poll_interval_s is not None else float(reply_sleep_s)
    if poll_interval_s is None and idle_timeout_s is None:
        timeout = float(reply_sleep_s) * int(reply_wait_iterations)
    elif idle_timeout_s is not None:
        timeout = float(idle_timeout_s)
    else:
        timeout = float(reply_sleep_s) * int(reply_wait_iterations)
    if interval <= 0:
        # A zero interval means "wake immediately"; lifetime then has to stay bounded by the
        # iteration count, otherwise a deadline-driven loop would spin forever.
        return 0.0, timeout, int(reply_wait_iterations)
    return interval, timeout, None


def _teammate_idle_hook(task_dir, task_name):
    """Self-driving teammate: claim the next open task, or report idle (CC m1575 qe / m0300 mat).

    When a claimable task exists it is claimed and the CC prompt is dropped into
    reply.txt so the next turn picks it up; otherwise an idle frame goes to the lead
    unless a lead DM already went out this round (CC m1448).
    """
    try:
        from genericagent.agents.idle import build_idle_notification, sent_lead_dm_since_last_turn
        from genericagent.agents.state import append_jsonl_event, append_parent_inbox_event
        from genericagent.agents.tasks import SubagentTaskStore, next_task_prompt
        from genericagent.agents.team import team_name_for

        state = read_json_or_none(os.path.join(task_dir, 'state.json')) or {}
        tasks_root = os.environ.get('GA_TASKS_ROOT')
        team_key = os.environ.get('GA_TEAM_KEY')
        if tasks_root and team_key:
            # The parent child-env snapshot is authoritative. Never infer the
            # task board root from task_dir depth when identity is present.
            store = SubagentTaskStore(
                os.environ.get('GA_PROJECT_ROOT') or task_dir,
                list_id=team_name_for(state.get('parent_session_id')),
                state_root=os.environ.get('GA_DATA_DIR') or os.path.dirname(os.path.dirname(task_dir)),
                tasks_root=tasks_root,
                team_key=team_key,
            )
        else:
            # Legacy task workers did not receive an identity snapshot.
            root_dir = os.path.dirname(os.path.dirname(task_dir))
            store = SubagentTaskStore(root_dir, list_id=team_name_for(state.get('parent_session_id')))
        events_path = os.path.join(task_dir, 'events.jsonl')
        # A completed turn is only a process fact, not proof that the assigned
        # task succeeded.  The teammate must call task_update(status="completed")
        # explicitly; close_agent reclaims unfinished work to pending.  Auto-
        # completing here made a BLOCKED teammate leave a false "completed" task
        # on the board and caused the lead to start a duplicate wave.
        task = store.find_claimable()
        if task:
            _claim_result = store.claim_task(task['id'], '/root/' + task_name, start=True)
            if not isinstance(_claim_result, dict) or not _claim_result.get('success'):
                return False
            claimed = _claim_result.get('task') or task
            with open(os.path.join(task_dir, 'reply.txt'), 'w', encoding='utf-8', newline=chr(10)) as handle:
                handle.write(next_task_prompt(claimed))
            append_jsonl_event(events_path, {
                'type': 'task_claimed',
                'task_name': task_name,
                'task_id': claimed.get('id'),
                'subject': claimed.get('subject'),
            })
            return True
        if not sent_lead_dm_since_last_turn(events_path):
            frame = build_idle_notification(
                from_name=task_name,
                idle_reason='available',
                summary='idle',
            )
            append_parent_inbox_event(task_dir, frame)
            append_jsonl_event(events_path, frame)
        return False
    except Exception:
        return False


def run_task_worker_loop(agent, task_dir, input_text=None, reply_wait_iterations=300, reply_sleep_s=2, sleep_fn=time.sleep, permission_profile=None, permission_options=None, parent_permission_mode=None, capability_options=None, poll_interval_s=None, idle_timeout_s=None, monotonic_fn=time.monotonic):
    task_dir = str(task_dir)
    task_name = os.path.basename(os.path.normpath(task_dir))
    _record_startup_phase('worker_entry', task_dir)
    poll_interval, idle_timeout, max_wait_iterations = resolve_reply_wait_schedule(
        reply_wait_iterations, reply_sleep_s, poll_interval_s, idle_timeout_s
    )
    agent.peer_hint = False
    # Process-backed workers are children even before their first turn.  The
    # capability boundary is applied later, after MCP schema warmup, so the
    # child receives every concrete non-orchestration tool but never the
    # parent-only orchestration surface.
    agent.is_subagent = True
    agent.capability_profile = None
    try:
        from genericagent.hooks.runtime import hook_runtime_for_agent, run_hook_event as _run_subagent_hook
        hook_runtime_for_agent(agent)
        _run_subagent_hook(agent, 'SubagentStart', {
            'task_name': task_name,
            'agent_type': 'subagent',
            'task_dir': task_dir,
            'permission_profile': permission_profile,
        })
    except Exception as e:
        print(f"[WARN] SubagentStart hooks failed: {e}")
    agent.capability_allow_delegation = False
    agent.capability_team_mode = False
    capability_options = capability_options if capability_options is not None else ((read_json_or_none(os.path.join(task_dir, 'state.json')) or {}).get('capability_options') or {})
    role_tools = capability_options.get('role_tools')
    if isinstance(role_tools, str):
        role_tools = [role_tools]
    agent.capability_role_tools = role_tools
    agent.capability_allow_delegation = bool(capability_options.get('allow_delegation', False))
    agent.capability_team_mode = bool(capability_options.get('team_mode', False))
    agent.task_dir = task_dir
    # Must run before anything writes state.json. Every writer in this loop rebuilds the file
    # from `read_json_or_none(...) or {}`, so the first one silently replaces unparsable bytes
    # — and with them run_id / parent_session_id / artifact_dir, i.e. the sidechain transcript
    # path and the artifact linkage. The turn then looks perfectly healthy.
    _quarantine_unreadable_state(task_dir, task_name)
    if permission_profile is not None or permission_options is not None or parent_permission_mode is not None:
        state_path = os.path.join(task_dir, 'state.json')
        state = read_json_or_none(state_path) or {}
        if permission_profile is not None:
            state['permission_profile'] = permission_profile
        if permission_options is not None:
            state['permission_options'] = permission_options
        if parent_permission_mode is not None:
            state['parent_permission_mode'] = parent_permission_mode
        atomic_write_json(state_path, state)
    permission_status = 'ok'
    permission_error = None
    try:
        from genericagent.agents.permissions import load_subagent_permission_policy
        agent.subagent_permission_policy = load_subagent_permission_policy(task_dir)
    except Exception as e:
        permission_status = 'error'
        permission_error = format_error(e)
        print(f"[WARN] Failed to attach subagent permission policy: {e}")
    _record_startup_phase('permission_ready', task_dir, status=permission_status, error=permission_error)
    state_path = os.path.join(task_dir, 'state.json')
    existing_state = read_json_or_none(state_path) or {}
    # Realtime subscription is an accelerator only: it carries change signals, while
    # mailbox.jsonl stays the authoritative source. A child that cannot subscribe keeps
    # polling, but records why — a silent child-side failure is how the never-connected
    # channel stayed hidden.
    realtime_subscriber = None
    child_ipc_status, child_ipc_fallback_reason = 'file', None
    try:
        from genericagent.agents.realtime_ipc import resolve_child_subscription
        realtime_subscriber, child_ipc_status, child_ipc_fallback_reason = resolve_child_subscription(task_dir, existing_state)
    except Exception as e:
        realtime_subscriber = None
        child_ipc_status, child_ipc_fallback_reason = 'fallback', format_error(e)
    _set_realtime_subscriber(realtime_subscriber)
    existing_state = _record_child_ipc_status(
        task_dir, task_name, child_ipc_status, child_ipc_fallback_reason, getattr(realtime_subscriber, 'address', None)
    )
    _record_startup_phase(
        'ipc_ready',
        task_dir,
        child_ipc_status=child_ipc_status,
        child_ipc_fallback_reason=child_ipc_fallback_reason,
    )
    nround = int(existing_state.get('round') or 0) if existing_state.get('round') else ''
    infile = os.path.join(task_dir, 'input.txt')
    if input_text:
        os.makedirs(task_dir, exist_ok=True)
        import glob
        [os.remove(f) for f in glob.glob(os.path.join(task_dir, 'output*.txt'))]
        with open(infile, 'w', encoding='utf-8') as f: f.write(input_text)
        nround = ''
    if (fh := consume_file(task_dir, '_history.json')):
        _load_forked_history(agent, task_dir, task_name, fh, nround)
    with open(infile, encoding='utf-8') as f: raw = f.read()
    _subagent_transcript_append(task_dir, 'request', {'task_name': task_name, 'prompt': raw})
    _record_startup_phase('request_loaded', task_dir)
    while True:
        output_path = os.path.join(task_dir, f'output{nround}.txt')
        try:
            _subagent_state(task_dir, task_name, nround, 'running', 'alive', output_path=output_path)
            _record_startup_phase('turn_state_written', task_dir)
            _subagent_event(task_dir, {'type': 'turn_started', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
            _record_startup_phase('turn_started_complete', task_dir)

            def _transcript_tool_before(tool_name, args, response):
                _subagent_transcript_append(
                    task_dir,
                    'tool_call',
                    {
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'tool_name': tool_name,
                        'args': {k: v for k, v in (args or {}).items() if not str(k).startswith('_')},
                    },
                )

            def _transcript_tool_after(tool_name, args, response, ret):
                data = getattr(ret, 'data', ret)
                if not isinstance(data, (dict, list, str, int, float, bool, type(None))):
                    data = {'content': getattr(data, 'content', str(data))}
                _subagent_transcript_append(
                    task_dir,
                    'tool_result',
                    {
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'tool_name': tool_name,
                        'data': data,
                    },
                )

            def _transcript_permission(tool_name, args, decision):
                _subagent_transcript_append(
                    task_dir,
                    'permission_decision',
                    {
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'tool_name': tool_name,
                        'decision': decision.to_dict(),
                    },
                )

            def _transcript_assistant(content, error=None):
                payload = {
                    'task_name': task_name,
                    'round': int(nround) if isinstance(nround, int) else 0,
                    'content': content,
                }
                if error:
                    payload['error'] = error
                _subagent_transcript_append(task_dir, 'assistant', payload)

            agent._subagent_transcript_tool_before = _transcript_tool_before
            agent._subagent_transcript_tool_after = _transcript_tool_after
            # Process subagents run GenericAgent.run(), so Stop would otherwise
            # be indistinguishable from the lead's Stop in a beep config. Mark
            # the event with the subagent identity; Stop remains emitted once per
            # process turn, while SubagentStop covers the full worker lifetime.
            agent.agent_id = task_name
            agent.agent_type = 'subagent'
            agent._subagent_transcript_permission = _transcript_permission
            agent._subagent_transcript_assistant = _transcript_assistant
            dq = agent.put_task(raw, source='task')
            while 'done' not in (item := dq.get(timeout=300)):
                if 'next' in item and random.random() < 0.95:
                    with open(output_path, 'w', encoding='utf-8') as f: f.write(item.get('next', ''))
                    _subagent_event(task_dir, {'type': 'output_snapshot', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0, 'output_path': output_path})
            with open(output_path, 'w', encoding='utf-8') as f: f.write(item['done'] + '\n\n[ROUND END]\n')
            digest = sha256_file(output_path)
            state_before_final = read_json_or_none(os.path.join(task_dir, 'state.json')) or {}
            final_output_ref = None
            try:
                from genericagent.agents.artifacts import SubagentArtifactStore
                run_id = state_before_final.get('run_id') or f"legacy_{task_name}"
                artifact_dir = state_before_final.get('artifact_dir') or os.path.join(os.path.dirname(task_dir), 'subagents', 'runs', run_id)
                artifact = SubagentArtifactStore(artifact_dir).record_final_output(output_path, round_no=int(nround) if isinstance(nround, int) else 0)
                final_output_ref = artifact.get('artifact_id')
                state_before_final['artifact_dir'] = artifact_dir
                state_before_final['final_output_ref'] = final_output_ref
                atomic_write_json(os.path.join(task_dir, 'state.json'), state_before_final)
            except Exception:
                pass
            try:
                _subagent_transcript_append(
                    task_dir,
                    'final_output',
                    {
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'output_path': output_path,
                        'sha256': digest,
                        'artifact_id': final_output_ref,
                        'final_output': item['done'],
                    },
                )
            except Exception:
                pass
            _subagent_state(task_dir, task_name, nround, 'completed', 'waiting_reply', output_path=output_path, final_output_path=output_path, final_output_sha256=digest)
            _subagent_event(task_dir, {'type': 'turn_completed', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0, 'output_path': output_path, 'sha256': digest, 'final_output_ref': final_output_ref})
            _subagent_event(task_dir, {'type': 'agent_waiting_reply', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
            consume_file(task_dir, '_stop')
            stop_requested = False
            if getattr(agent, 'capability_team_mode', False):
                # A teammate never idles while work is claimable (CC m1575 qe).
                idle_state = {'blocked': False}
                try:
                    from genericagent.agents.team import team_name_for as _team_name_for
                    from genericagent.hooks.runtime import run_hook_event as _run_idle_hook
                    idle = _run_idle_hook(agent, 'TeammateIdle', {
                        'teammate_name': task_name,
                        'team_name': _team_name_for(task_dir),
                    })
                    idle_state['blocked'] = idle.blocked
                    if idle.system_text:
                        _subagent_event(task_dir, {
                            'type': 'hook_system_message',
                            'task_name': task_name,
                            'round': int(nround) if isinstance(nround, int) else 0,
                            'message': idle.system_text,
                        })
                except Exception as hook_err:
                    print(f"[WARN] TeammateIdle hooks failed: {hook_err}")
                if not idle_state['blocked']:
                    _teammate_idle_hook(task_dir, task_name)
            # Provenance + receipts for the durable file bus (CC m0300 / m0188): a row that
            # was not written through the ledger is not a genuine teammate message.
            try:
                from genericagent.agents.receipts import ProvenanceLedger, build_roster

                _prov_ledger = ProvenanceLedger(os.path.join(os.path.dirname(task_dir), 'subagents'))
                _roster = build_roster(os.path.dirname(task_dir))
            except Exception:
                _prov_ledger, _roster = None, None

            def _consume_child_mailbox():
                try:
                    from genericagent.agents.mailbox import SubagentMailbox

                    return SubagentMailbox(os.path.join(task_dir, 'mailbox.jsonl')).consume_trigger_turn(
                        provenance=_prov_ledger, roster=_roster
                    )
                except Exception:
                    return None

            for wait_s in _reply_wait_slices(poll_interval, idle_timeout, max_wait_iterations, monotonic_fn):
                if realtime_subscriber is not None and not realtime_subscriber.closed:
                    # A signal carries no payload; it only means "go re-read the durable
                    # mailbox now" instead of waiting out the poll interval.
                    realtime_subscriber.wait(wait_s)
                else:
                    sleep_fn(wait_s)
                if realtime_subscriber is not None and realtime_subscriber.closed and child_ipc_status != 'fallback':
                    # The parent tore the channel down mid-run. Checked here rather than inside
                    # the wait branch because the disconnect is now just as likely to surface on
                    # an outbound signal from _subagent_event. Polling still delivers, but the
                    # degradation has to be visible or it looks healthy.
                    child_ipc_status, child_ipc_fallback_reason = 'fallback', 'realtime channel disconnected mid-run; durable file event bus stays the transport'
                    _record_child_ipc_status(task_dir, task_name, child_ipc_status, child_ipc_fallback_reason)
                mailbox_result = _consume_child_mailbox()
                if mailbox_result is not None:
                    raw = mailbox_result.get('content') or ''
                    consume_file(task_dir, 'reply.txt')
                    _subagent_event(task_dir, {
                        'type': 'message_consumed',
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'source': 'mailbox',
                        'message_ids': [row.get('message_id') for row in mailbox_result.get('messages') or []],
                    })
                    break
                if consume_file(task_dir, '_stop'):
                    # CC m1356: a queued message is delivered ahead of a shutdown request,
                    # so only stop once nothing is left to deliver.
                    _subagent_state(task_dir, task_name, nround, 'completed', 'shutdown', output_path=output_path, final_output_path=output_path, final_output_sha256=digest)
                    _subagent_event(task_dir, {'type': 'agent_shutdown', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
                    stop_requested = True
                    break
                if (raw := consume_file(task_dir, 'reply.txt')):
                    _subagent_event(task_dir, {'type': 'message_consumed', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
                    break
            else:
                _subagent_state(task_dir, task_name, nround, 'completed', 'exited', output_path=output_path, final_output_path=output_path, final_output_sha256=digest)
                _subagent_event(task_dir, {'type': 'agent_exited', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
                break
            if stop_requested:
                break
            nround = nround + 1 if isinstance(nround, int) else 1
        except Exception as e:
            _subagent_state(task_dir, task_name, nround, 'errored', 'exited', output_path=output_path, last_error=format_error(e), last_error_stage='turn')
            _subagent_event(task_dir, {'type': 'agent_error', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0, 'error': format_error(e)})
            try:
                from genericagent.hooks.runtime import run_hook_event as _run_subagent_hook
                _run_subagent_hook(agent, 'SubagentStop', {
                    'task_name': task_name,
                    'task_dir': task_dir,
                    'exit_reason': 'failed',
                    'error': format_error(e),
                })
            except Exception as hook_err:
                print(f"[WARN] SubagentStop hooks failed: {hook_err}")
            if realtime_subscriber is not None:
                realtime_subscriber.close()
            _set_realtime_subscriber(None)
            raise
    try:
        from genericagent.hooks.runtime import run_hook_event as _run_subagent_hook
        _run_subagent_hook(agent, 'SubagentStop', {
            'task_name': task_name,
            'task_dir': task_dir,
            'exit_reason': 'shutdown' if stop_requested else 'exited',
        })
    except Exception as hook_err:
        print(f"[WARN] SubagentStop hooks failed: {hook_err}")
    if realtime_subscriber is not None:
        realtime_subscriber.close()
    _set_realtime_subscriber(None)
    _release_subagent_slot(task_dir)

def start_task_background(
    task_name,
    input_text=None,
    *,
    llm_no=0,
    verbose=False,
    root_dir=None,
    project_root=None,
    state_root=None,
    task_dir=None,
    popen=None,
    python_executable=None,
    fork_turns=None,
    fork_history=None,
    permission_profile=None,
    parent_permission_mode=None,
    permission_options=None,
    capability_options=None,
):
    from genericagent.agents.manager import SubagentManager

    legacy_root = os.path.abspath(root_dir or str(paths.RUNTIME_ROOT))
    project_path = os.path.abspath(project_root or legacy_root)
    state_path_root = os.path.abspath(state_root or legacy_root)
    task_dir = os.path.abspath(task_dir or os.path.join(state_path_root, 'temp', task_name))
    manager = SubagentManager(
        root_dir=legacy_root,
        project_root=project_path,
        project_state_root=state_path_root,
        popen=popen,
        python_executable=python_executable or sys.executable,
    )
    task_name = manager._task_name_from_target(task_name)
    if fork_turns is None:
        fork_turns = 'all' if fork_history is not None else 'none'
    fork_mode, history_to_write = manager._select_fork_history(fork_turns, fork_history)
    from genericagent.agents.permissions import normalize_permission_metadata
    permission_metadata = normalize_permission_metadata(
        {'permission_profile': permission_profile, 'permission_options': permission_options or {}, 'parent_permission_mode': parent_permission_mode}
    )
    os.makedirs(task_dir, exist_ok=True)
    input_path = os.path.join(task_dir, 'input.txt')
    output_path = os.path.join(task_dir, 'output.txt')
    if input_text is not None:
        import glob

        for old_output in glob.glob(os.path.join(task_dir, 'output*.txt')):
            try:
                os.remove(old_output)
            except OSError:
                pass
        with open(input_path, 'w', encoding='utf-8') as f:
            f.write(input_text)
    history_path = os.path.join(task_dir, '_history.json')
    if os.path.exists(history_path):
        os.remove(history_path)
    if history_to_write is not None:
        atomic_write_json(history_path, history_to_write)
    state = {
        'schema_version': 1,
        'task_name': task_name,
        'agent_path': f'/root/{task_name}',
        'parent_session_id': None,
        'pid': None,
        'round': 0,
        'turn_status': 'pending',
        'process_status': 'starting',
        'started_at': now_iso(),
        'updated_at': now_iso(),
        'input_path': input_path,
        'output_path': output_path,
        'final_output_path': None,
        'final_output_sha256': None,
        'last_message': input_text,
        'last_error': None,
        'close_reason': None,
        'fork_turns': fork_mode,
        'permission_profile': permission_metadata['permission_profile'],
        'parent_permission_mode': permission_metadata.get('parent_permission_mode'),
        'permission_options': permission_metadata['options'],
        'capability_options': capability_options or {},
        'capability_profile': 'delegating' if (capability_options or {}).get('allow_delegation') else 'isolated',
        'allow_delegation': bool((capability_options or {}).get('allow_delegation', False)),
        'context_mode': 'isolated' if fork_mode == 'none' else 'explicit_fork',
    }
    state_path = os.path.join(task_dir, 'state.json')
    atomic_write_json(state_path, state)

    cmd = [
        manager.python_executable,
        os.path.abspath(__file__),
        '--task',
        task_name,
        '--nobg',
        '--task_dir',
        task_dir,
        '--project_root',
        project_path,
        '--state_root',
        state_path_root,
        '--llm_no',
        str(llm_no),
        '--permission_profile',
        permission_metadata['permission_profile'],
        '--parent_permission_mode',
        permission_metadata.get('parent_permission_mode') or '',
        '--permission_options',
        json.dumps(permission_metadata['options'], ensure_ascii=False, sort_keys=True),
        '--capability_options',
        json.dumps(capability_options or {}, ensure_ascii=False, sort_keys=True),
    ]
    if verbose:
        cmd.append('--verbose')

    stdout = open(os.path.join(task_dir, 'stdout.log'), 'w', encoding='utf-8')
    stderr = open(os.path.join(task_dir, 'stderr.log'), 'w', encoding='utf-8')
    try:
        # A parent bridge may be blocked reading its stdin pipe.  Do not inherit that handle:
        # on Windows the child can otherwise remain in process startup before process_entry.
        roots = paths.project_roots(project=project_path, data_root=state_path_root)
        child_env = {
            **os.environ,
            'GA_PROJECT_ROOT': str(roots.project_root),
            'GA_PROJECT_KEY': roots.project_key,
            'GA_PROJECT_DATA_DIR': str(roots.project_data_dir),
            'GA_DATA_DIR': str(roots.data_root),
            'GA_TOOL_CWD': str(roots.project_root),
        }
        kwargs = {'cwd': project_path, 'env': child_env, 'stdout': stdout, 'stderr': stderr, 'stdin': subprocess.DEVNULL}
        if os.name == 'nt':
            kwargs['creationflags'] = 0x08000000
        proc = (popen or __import__('subprocess').Popen)(cmd, **kwargs)
    except Exception as e:
        state.update({'turn_status': 'errored', 'process_status': 'exited', 'last_error': format_error(e), 'updated_at': now_iso()})
        atomic_write_json(state_path, state)
        _subagent_event(task_dir, {'type': 'agent_error', 'task_name': task_name, 'round': 0, 'error': format_error(e)})
        manager.register_agent(task_name, state, task_dir)
        raise
    finally:
        stdout.close()
        stderr.close()

    pid = getattr(proc, 'pid', None)
    state.update({'pid': pid, 'process_status': 'alive', 'updated_at': now_iso()})
    atomic_write_json(state_path, state)
    _subagent_event(task_dir, {'type': 'agent_started', 'task_name': task_name, 'pid': pid})
    manager.register_agent(task_name, state, task_dir)
    return pid

def _load_permission_options_arg(value):
    if not value:
        return None
    try:
        loaded = json.loads(value)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_fork_history_arg(value):
    if not value:
        return None
    source = value[1:] if value.startswith('@') else value
    if os.path.exists(source):
        with open(source, 'r', encoding='utf-8') as f:
            payload = f.read()
    else:
        payload = value
    return json.loads(payload)

if __name__ == '__main__':
    import argparse
    from datetime import datetime
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', metavar='IODIR', help='一次性任务模式(文件IO)')
    parser.add_argument('--reflect', metavar='SCRIPT', help='反射模式：加载监控脚本，check()触发时发任务')
    parser.add_argument('--input', help='prompt')
    parser.add_argument('--task_root', default=None, help='legacy task/state root alias')
    parser.add_argument('--project_root', default=None, help='project workspace for the child process')
    parser.add_argument('--state_root', default=None, help='GA runtime state root')
    parser.add_argument('--task_dir', default=None, help='explicit task I/O directory')
    parser.add_argument('--llm_no', type=int, default=0)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--nobg', action='store_true')
    parser.add_argument('--fork_turns', default=None, help='context fork mode: none, all, or a positive integer')
    parser.add_argument('--fork_history', default=None, help='context fork history as JSON, a JSON file path, or @file')
    parser.add_argument('--permission_profile', default=None, help='subagent permission profile')
    parser.add_argument('--parent_permission_mode', default=None, help='parent permission mode for inherit-current-permissions')
    parser.add_argument('--permission_options', default=None, help='subagent permission options JSON')
    parser.add_argument('--capability_options', default=None, help='subagent capability options JSON')
    args, _unknown = parser.parse_known_args()
    _record_startup_phase('arguments_parsed')
    _reflect_args = dict(zip([k.lstrip('-') for k in _unknown[::2]], _unknown[1::2])) if _unknown else {}

    if args.task and not args.nobg:
        fork_history = _load_fork_history_arg(args.fork_history)
        print(start_task_background(
            args.task,
            input_text=args.input,
            llm_no=args.llm_no,
            verbose=args.verbose,
            root_dir=args.task_root,
            fork_turns=args.fork_turns,
            fork_history=fork_history,
            permission_profile=args.permission_profile,
            parent_permission_mode=args.parent_permission_mode,
            permission_options=_load_permission_options_arg(args.permission_options),
            capability_options=_load_permission_options_arg(args.capability_options),
        )); sys.exit(0)

    _record_startup_phase('agent_init_started')
    agent = GeneraticAgent()
    _record_startup_phase('agent_init_complete')
    start_mcp_schema_warmup()
    agent.next_llm(args.llm_no)
    selected_backend = getattr(getattr(agent, 'llmclient', None), 'backend', None)
    _record_startup_phase(
        'model_selected',
        llm_no=agent.llm_no,
        model_profile=getattr(selected_backend, 'name', None),
        model_id=getattr(selected_backend, 'model', None),
    )
    agent.verbose = args.verbose
    threading.Thread(target=agent.run, daemon=True).start()
    _record_startup_phase('agent_thread_started')

    if args.task:
        task_dir = (
            os.path.abspath(args.task_dir)
            if args.task_dir
            else os.path.join(os.path.abspath(args.state_root or args.task_root or str(paths.RUNTIME_ROOT)), 'temp', args.task)
        )
        run_task_worker_loop(
            agent,
            task_dir,
            input_text=args.input,
            permission_profile=args.permission_profile,
            parent_permission_mode=args.parent_permission_mode,
            permission_options=_load_permission_options_arg(args.permission_options),
            capability_options=_load_permission_options_arg(args.capability_options),
            **resolve_reply_wait_schedule_from_env(),
        )
    elif args.reflect:
        agent.peer_hint = False
        import importlib.util
        spec = importlib.util.spec_from_file_location('reflect_script', args.reflect)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        if hasattr(mod, 'init'): mod.init(_reflect_args)
        _mt = os.path.getmtime(args.reflect)
        print(f'[Reflect] loaded {args.reflect}' + (f' args={_reflect_args}' if _reflect_args else ''))
        while True:
            if os.path.getmtime(args.reflect) != _mt:
                try:
                    spec.loader.exec_module(mod); _mt = os.path.getmtime(args.reflect)
                    if hasattr(mod, 'init'): mod.init(_reflect_args)
                    print('[Reflect] reloaded')
                except Exception as e: print(f'[Reflect] reload error: {e}')
            time.sleep(getattr(mod, 'INTERVAL', 5))
            try: task = mod.check()
            except Exception as e: 
                print(f'[Reflect] check() error: {e}'); continue
            if task and task == '/exit': break
            if task is None: continue
            print(f'[Reflect] triggered: {task[:80]}')
            dq = agent.put_task(task, source='reflect')
            try:
                while 'done' not in (item := dq.get(timeout=180)): pass
                result = item['done']
                print(result)
            except Exception as e:
                if getattr(mod, 'ONCE', False): raise
                print(f'[Reflect] drain error: {e}'); result = f'[ERROR] {e}'
            log_dir = os.path.join(str(paths.TEMP_DIR), 'reflect_logs'); os.makedirs(log_dir, exist_ok=True)
            script_name = os.path.splitext(os.path.basename(args.reflect))[0]
            open(os.path.join(log_dir, f'{script_name}_{datetime.now():%Y-%m-%d}.log'), 'a', encoding='utf-8').write(f'[{datetime.now():%m-%d %H:%M}]\n{result}\n\n')
            if (on_done := getattr(mod, 'on_done', None)):
                try: on_done(result)
                except Exception as e: print(f'[Reflect] on_done error: {e}')
            if getattr(mod, 'ONCE', False): print('[Reflect] ONCE=True, exiting.'); break
    else:
        try: import readline
        except Exception: pass
        agent.inc_out = True
        while True:
            q = input('> ').strip()
            if not q: continue
            try:
                dq = agent.put_task(q, source='user')
                while True:
                    item = dq.get()
                    if 'next' in item: print(item['next'], end='', flush=True)
                    if 'done' in item: print(); break
            except KeyboardInterrupt:
                agent.abort()
                print('\n[Interrupted]')
