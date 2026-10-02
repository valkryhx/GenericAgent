from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from sensitive_redaction import redact_sensitive_text
from workflow_workspace import resolve_workspace_child


MAX_OUTPUT_CHARS = 20_000


def _workspace_path(workspace):
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("workflow check workspace must be an existing directory")
    return path


def _safe_workspace_child(workspace, raw):
    try:
        return resolve_workspace_child(raw, workspace)
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact path must be a non-empty path within workspace") from exc


def _bounded(value):
    text = redact_sensitive_text(str(value or ""))
    return {"text": text[:MAX_OUTPUT_CHARS], "truncated": len(text) > MAX_OUTPUT_CHARS}


def _command_argv(check):
    command = check.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("verification command must be an argv list")

    argv = list(command)
    executable = Path(argv[0]).name.lower()
    python_names = {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}
    if executable in python_names:
        allowed_modules = {"unittest", "pytest", "compileall"}
        if len(argv) < 3 or argv[1] != "-m" or argv[2] not in allowed_modules:
            raise ValueError("verification command is not allowlisted")
    elif executable in {"pytest", "pytest.exe", "ruff", "ruff.exe", "mypy", "mypy.exe"}:
        pass
    elif executable in {"git", "git.exe"} and argv[1:] == ["diff", "--check"]:
        pass
    else:
        raise ValueError("verification command is not allowlisted")
    return argv


def _run_command(check, workspace, timeout_s):
    argv = _command_argv(check)
    started = time.time()
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=max(0.1, float(timeout_s)),
            shell=False,
            check=False,
        )
        stdout = _bounded(completed.stdout)
        stderr = _bounded(completed.stderr)
        return {
            "status": "passed" if completed.returncode == 0 else "failed",
            "evidence": {
                "argv": argv,
                "exitCode": completed.returncode,
                "stdout": stdout["text"],
                "stderr": stderr["text"],
                "stdoutTruncated": stdout["truncated"],
                "stderrTruncated": stderr["truncated"],
                "timedOut": False,
                "durationMs": round((time.time() - started) * 1000, 2),
                "commandFingerprint": hashlib.sha256(json.dumps(argv).encode("utf-8")).hexdigest(),
            },
        }
    except subprocess.TimeoutExpired as exc:
        stdout = _bounded(exc.stdout)
        stderr = _bounded(exc.stderr)
        return {
            "status": "failed",
            "evidence": {
                "argv": argv,
                "exitCode": None,
                "stdout": stdout["text"],
                "stderr": stderr["text"],
                "stdoutTruncated": stdout["truncated"],
                "stderrTruncated": stderr["truncated"],
                "timedOut": True,
                "durationMs": round((time.time() - started) * 1000, 2),
                "commandFingerprint": hashlib.sha256(json.dumps(argv).encode("utf-8")).hexdigest(),
            },
        }


def _schema_check(check):
    evidence = check.get("evidence")
    if not isinstance(evidence, dict):
        return {"status": "failed", "evidence": {"schemaRef": check.get("schemaRef"), "reason": "missing evidence"}}
    required = check.get("requiredFields") or ["verificationPassed", "checks", "blockingIssues"]
    missing = [field for field in required if field not in evidence]
    return {
        "status": "passed" if not missing and evidence.get("verificationPassed", True) is not False else "failed",
        "evidence": {"schemaRef": check.get("schemaRef"), "missingFields": missing, "payload": evidence},
    }


def _artifact_check(check, workspace):
    path = _safe_workspace_child(workspace, check.get("path"))
    exists = path.is_file() if check.get("file", True) else path.exists()
    relative = str(path.relative_to(workspace)).replace("\\", "/")
    evidence = {"path": relative, "exists": exists, "size": path.stat().st_size if exists and path.is_file() else 0}
    if not exists or not path.is_file():
        return {"status": "failed", "evidence": evidence}

    raw = path.read_bytes()
    # ``id`` must be unique per verification contract, but the machine
    # semantics belong to the declared check name. A derived check may carry
    # ``check: artifact_readback`` with an artifact-qualified id such as
    # ``page_artifact_readback``.
    check_id = str(check.get("check") or check.get("id") or "").strip().lower()
    if check_id == "artifact_readback":
        evidence["readable"] = True
        evidence["nonEmpty"] = bool(raw.strip())
        evidence["bytesRead"] = len(raw)
        return {"status": "passed" if raw.strip() else "failed", "evidence": evidence}
    if check_id == "artifact_structure":
        artifact_format = _artifact_format(check, path)
        evidence["format"] = artifact_format
        strict = check.get("strict") is True or check.get("contentValidation") is True
        if not strict:
            evidence["validationMode"] = "observational"
            evidence["nonEmpty"] = bool(raw.strip())
            evidence["contentValidation"] = "not_requested"
            return {"status": "passed" if evidence["nonEmpty"] else "failed", "evidence": evidence}
        evidence["validationMode"] = "strict"
        if artifact_format == "docx":
            try:
                with zipfile.ZipFile(path) as package:
                    bad_entry = package.testzip()
                    names = set(package.namelist())
                required_entries = ["[Content_Types].xml", "word/document.xml"]
                missing_entries = [name for name in required_entries if name not in names]
                evidence["zipValid"] = bad_entry is None
                evidence["corruptEntry"] = bad_entry
                evidence["requiredEntries"] = required_entries
                evidence["missingEntries"] = missing_entries
                passed = bad_entry is None and not missing_entries
            except (OSError, zipfile.BadZipFile):
                evidence["zipValid"] = False
                evidence["requiredEntries"] = ["[Content_Types].xml", "word/document.xml"]
                evidence["missingEntries"] = evidence["requiredEntries"]
                passed = False
            return {"status": "passed" if passed else "failed", "evidence": evidence}
        if artifact_format == "zip":
            try:
                with zipfile.ZipFile(path) as package:
                    bad_entry = package.testzip()
                    evidence["zipValid"] = bad_entry is None
                    evidence["entryCount"] = len(package.namelist())
                    evidence["corruptEntry"] = bad_entry
                passed = evidence["zipValid"] and evidence["entryCount"] > 0
            except (OSError, zipfile.BadZipFile):
                evidence["zipValid"] = False
                passed = False
            return {"status": "passed" if passed else "failed", "evidence": evidence}
        if artifact_format == "pdf":
            evidence["pdfMagic"] = raw.startswith(b"%PDF-")
            return {"status": "passed" if evidence["pdfMagic"] else "failed", "evidence": evidence}

        text = raw.decode("utf-8", errors="replace")
        evidence["textReadable"] = bool(text.strip())
        if artifact_format == "html":
            parser = _CountingHtmlParser()
            try:
                parser.feed(text)
                parser.close()
            except Exception as exc:
                evidence["parserError"] = str(exc)[:200]
            evidence["startTagCount"] = parser.start_tag_count
            evidence["htmlStructure"] = bool(text.strip()) and parser.start_tag_count > 0
            return {"status": "passed" if evidence["htmlStructure"] else "failed", "evidence": evidence}
        if artifact_format == "json":
            try:
                json.loads(text)
                evidence["jsonValid"] = True
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                evidence["jsonValid"] = False
                evidence["parserError"] = str(exc)[:200]
            return {"status": "passed" if evidence["jsonValid"] else "failed", "evidence": evidence}
        if artifact_format == "xml":
            try:
                ElementTree.fromstring(text)
                evidence["xmlValid"] = True
            except (ElementTree.ParseError, ValueError) as exc:
                evidence["xmlValid"] = False
                evidence["parserError"] = str(exc)[:200]
            return {"status": "passed" if evidence["xmlValid"] else "failed", "evidence": evidence}

        evidence["nonEmpty"] = bool(raw.strip())
        return {"status": "passed" if evidence["nonEmpty"] else "failed", "evidence": evidence}
    if check_id == "source_count":
        artifact_format = _artifact_format(check, path)
        strict = check.get("strict") is True or check.get("contentValidation") is True
        if not strict:
            evidence["format"] = artifact_format
            evidence["validationMode"] = "observational"
            evidence["nonEmpty"] = bool(raw.strip())
            evidence["contentValidation"] = "not_requested"
            return {"status": "passed" if evidence["nonEmpty"] else "failed", "evidence": evidence}
        evidence["validationMode"] = "strict"
        text = _read_artifact_text(path, artifact_format, raw)
        urls = sorted(set(re.findall(r"https?://[^\s<>\"']+", text, re.I)))
        minimum = max(1, int(check.get("minimum") or check.get("minCount") or 2))
        source_entries = _docx_source_entries(text) if not urls and artifact_format == "docx" else []
        evidence["sourceCount"] = len(urls) if urls else len(source_entries)
        evidence["minimum"] = minimum
        evidence["sources"] = urls[:50]
        evidence["sourceCountMode"] = "urls" if urls else "document_entries"
        if source_entries:
            evidence["sourceEntries"] = source_entries[:50]
        return {"status": "passed" if evidence["sourceCount"] >= minimum else "failed", "evidence": evidence}
    return {"status": "passed", "evidence": evidence}


class _CountingHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.start_tag_count = 0

    def handle_starttag(self, _tag, _attrs):
        self.start_tag_count += 1


def _artifact_format(check, path):
    explicit = str(
        check.get("artifactType")
        or check.get("format")
        or check.get("mimeType")
        or ""
    ).strip().lower()
    aliases = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/zip": "zip",
        "text/html": "html",
        "application/json": "json",
        "application/pdf": "pdf",
        "text/xml": "xml",
        "application/xml": "xml",
    }
    if explicit in aliases:
        return aliases[explicit]
    if explicit in {"docx", "zip", "html", "htm", "json", "pdf", "xml", "text", "binary"}:
        return "html" if explicit == "htm" else explicit
    suffix = path.suffix.lower()
    return {
        ".docx": "docx",
        ".zip": "zip",
        ".html": "html",
        ".htm": "html",
        ".json": "json",
        ".pdf": "pdf",
        ".xml": "xml",
    }.get(suffix, "unknown")


def _read_artifact_text(path, artifact_format, raw):
    if artifact_format != "docx":
        return raw.decode("utf-8", errors="replace")
    try:
        with zipfile.ZipFile(path) as package:
            root = ElementTree.fromstring(package.read("word/document.xml"))
        paragraphs = []
        for paragraph in root.findall(".//{*}p"):
            paragraphs.append("".join(text.text or "" for text in paragraph.findall(".//{*}t")))
        return "\n".join(paragraphs)
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return ""


def _docx_source_entries(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    headings = {"sources", "references", "来源", "参考文献", "引用来源"}
    start = next(
        (index + 1 for index, line in enumerate(lines) if line.casefold() in headings),
        None,
    )
    if start is None:
        return []
    entries = []
    for line in lines[start:]:
        if line.casefold() in {"conclusion", "结论", "appendix", "附录"}:
            break
        entries.append(line)
    return entries


def run_check(check, *, workspace, timeout_s=30):
    if not isinstance(check, dict):
        raise ValueError("verification check must be an object")
    check_id = str(check.get("id") or "").strip()
    if not check_id:
        raise ValueError("verification check requires id")
    workspace_path = _workspace_path(workspace)
    kind = str(check.get("kind") or "").strip().lower()
    if kind == "command":
        result = _run_command(check, workspace_path, timeout_s)
    elif kind == "schema":
        result = _schema_check(check)
    elif kind == "artifact":
        result = _artifact_check(check, workspace_path)
    elif kind in {"diff", "review", "manual"}:
        result = {"status": "skipped", "evidence": {"reason": f"{kind} evidence must be supplied by runtime owner"}}
    else:
        raise ValueError(f"unsupported verification check kind: {kind}")
    result["checkId"] = check_id
    result["kind"] = kind
    return result
