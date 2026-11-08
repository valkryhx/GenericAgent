"""Find which fakeredis releases still expose ``aioredis.FakeServer``.

fastmcp 2.14.4 -> pydocket (<0.17.0) -> ``from fakeredis.aioredis import
FakeServer``. pydocket declares only ``fakeredis[lua]>=2.32.1`` with no upper
bound, so a fresh install can pick a fakeredis that renamed the symbol and
every MCP stdio fixture in the suite fails to start.

This walks the candidate versions, inspects each wheel's
``fakeredis/aioredis.py`` without installing it, and reports the newest release
that still defines ``FakeServer``. That number is what GA's pyproject should
pin.
"""

import io
import re
import sys
import urllib.request
import zipfile

INDEX = "https://pypi.org/pypi/fakeredis/json"


def versions():
    with urllib.request.urlopen(INDEX, timeout=60) as resp:
        import json

        data = json.load(resp)
    names = [v for v in data["releases"] if re.fullmatch(r"2\.(3[2-9]|[4-9]\d)\.\d+", v)]
    # Newest first; a plain tuple sort is enough for these two-part versions.
    return sorted(names, key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)


def has_fake_server(version):
    """Whether ``from fakeredis.aioredis import FakeServer`` works in this release.

    The name is not necessarily *defined* in ``aioredis.py`` -- in 2.32.1 it is
    imported from ``._server`` -- so looking for ``class FakeServer`` gives a
    false negative. What matters is whether the symbol is bound in the
    ``fakeredis.aioredis`` namespace, which is exactly what pydocket's import
    needs. Check both the definition sites and the import line.
    """
    url = f"https://pypi.org/pypi/fakeredis/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as resp:
        import json

        meta = json.load(resp)
    wheel = next(
        (u for u in meta["urls"] if u["filename"].endswith(".whl") and "py3" in u["filename"]),
        None,
    )
    if wheel is None:
        return None
    with urllib.request.urlopen(wheel["url"], timeout=120) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        src = zf.read("fakeredis/aioredis.py").decode("utf-8", "replace")
        server_src = ""
        if "fakeredis/_server.py" in names:
            server_src = zf.read("fakeredis/_server.py").decode("utf-8", "replace")

    # 1) imported into the aioredis namespace, or 2) defined there directly.
    imported = re.search(r"^from\s+\S+\s+import\s+.*\bFakeServer\b", src, re.MULTILINE)
    defined = re.search(r"^\s*(class FakeServer\b|FakeServer\s*=)", src, re.MULTILINE)
    # 3) or defined in _server.py and re-exported through the import above.
    defined_elsewhere = re.search(r"^\s*class FakeServer\b", server_src, re.MULTILINE)
    return bool((imported and defined_elsewhere) or defined)


def main():
    found = []
    for version in versions():
        try:
            ok = has_fake_server(version)
        except Exception as exc:  # network hiccup on one version is not fatal
            print(f"{version:<10} error: {exc}")
            continue
        print(f"{version:<10} FakeServer={ok}")
        found.append((version, ok))
        if ok is False and found and any(v for v, k in found[:-1] if k):
            break

    good = [v for v, k in found if k]
    print()
    print("newest with FakeServer:", good[0] if good else "<none>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
