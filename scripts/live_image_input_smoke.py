"""Live smoke: native image input via llm.yaml models (path + in-memory clipboard-style).

Usage (from repo root):
  python scripts/live_image_input_smoke.py
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

IMAGE_PATH = REPO / "截图" / "图2.png"
MODELS = ["grok-4.5", "gpt-5.6-terra"]
PROMPT = "用一句话描述这张图片里主要有什么（中文）。不要编造看不清的细节。"


def main() -> int:
    from llm_client import load_clients_from_yaml
    from agentmain import supports_image_input, _build_user_content_with_images
    from image_codec import encode_image_for_prompt
    from llmcore import _msgs_claude2oai, _to_responses_input

    if not IMAGE_PATH.is_file():
        print(f"FAIL: image missing: {IMAGE_PATH}")
        return 2

    clients, active, cfg_path, _ = load_clients_from_yaml()
    print(f"config: {cfg_path}")
    print(f"image: {IMAGE_PATH} ({IMAGE_PATH.stat().st_size} bytes)")

    # name map: backend.name often like profile or session name
    by_key = {}
    for c in clients:
        b = getattr(c, "backend", None)
        b = getattr(b, "primary", b)
        name = getattr(b, "name", "") or ""
        model = getattr(b, "model", "") or ""
        # profiles are often named after model key
        by_key[name] = c
        by_key[model] = c
        print(
            f"  client name={name!r} model={model!r} "
            f"native_image_input={getattr(b, 'native_image_input', None)} "
            f"supports_image_input(fn)={supports_image_input(c)}"
        )

    # codec smoke (path)
    enc = encode_image_for_prompt(str(IMAGE_PATH))
    print(
        f"codec path: media={enc.media_type} {enc.width}x{enc.height} "
        f"b64_len={len(enc.data_b64)} resized={enc.resized}"
    )

    # clipboard-style: copy bytes to temp as if paste landed a file
    clip_path = None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(IMAGE_PATH.read_bytes())
        clip_path = f.name
    print(f"clipboard-style temp: {clip_path}")

    # URL note probe: current builder only local files
    url_content = _build_user_content_with_images(
        "https://example.com/demo.png 描述",
        images=["https://example.com/demo.png"],
    )
    print(f"URL as images= : content is None? {url_content is None}  (expected True / no remote fetch)")

    results = []
    for key in MODELS:
        client = by_key.get(key)
        if client is None:
            # try fuzzy
            for n, c in by_key.items():
                if key in str(n):
                    client = c
                    break
        if client is None:
            print(f"\n=== {key}: NOT FOUND in loaded clients ===")
            results.append((key, "missing", None))
            continue

        backend = getattr(client, "backend", None)
        backend = getattr(backend, "primary", backend)
        print(f"\n=== {key} ===")
        print(
            f"model={getattr(backend, 'model', None)} "
            f"api_mode={getattr(backend, 'api_mode', None)} "
            f"native_image_input={getattr(backend, 'native_image_input', None)} "
            f"supports={supports_image_input(client)}"
        )

        if not supports_image_input(client):
            print("SKIP live ask: supports_image_input=False (gate closed)")
            results.append((key, "gate_closed", None))
            continue

        for label, img in (("path", str(IMAGE_PATH)), ("clipboard_file", clip_path)):
            content = _build_user_content_with_images(PROMPT, images=[img])
            if not content or not any(b.get("type") == "image" for b in content):
                print(f"  [{label}] FAIL build content")
                results.append((key, f"{label}_build_fail", None))
                continue
            # protocol convert smoke
            oai = _msgs_claude2oai([{"role": "user", "content": content}])
            has_url = False
            if oai and isinstance(oai[0].get("content"), list):
                has_url = any(p.get("type") == "image_url" for p in oai[0]["content"] if isinstance(p, dict))
            print(f"  [{label}] blocks={len(content)} oai_image_url={has_url}")

            # live call: single turn, no tools
            backend.history = []
            backend.system = "You are a vision checker. Answer briefly in Chinese."
            backend.tools = None
            try:
                # NativeToolClient.chat expects messages
                gen = client.chat(
                    messages=[
                        {"role": "system", "content": backend.system},
                        {"role": "user", "content": content},
                    ],
                    tools=None,
                )
                chunks = []
                resp = None
                try:
                    while True:
                        chunks.append(next(gen))
                except StopIteration as e:
                    resp = e.value
                text = "".join(chunks).strip()
                if resp is not None and getattr(resp, "content", None):
                    text = (text or resp.content or "").strip()
                preview = (text or "")[:300].replace("\n", " ")
                err = text.startswith("!!!Error:") if text else True
                print(f"  [{label}] {'ERROR' if err else 'OK'}: {preview!r}")
                results.append((key, label, "error" if err else "ok", preview))
            except Exception as e:
                print(f"  [{label}] EXCEPTION: {e}")
                results.append((key, label, "exception", str(e)[:200]))

    try:
        os.unlink(clip_path)
    except OSError:
        pass

    print("\n=== SUMMARY ===")
    for row in results:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
