"""Probe: does run_id alone collide across managers with different roots?

Windows named pipes are one global namespace and the address used to be built
from run_id only, while every manager numbers its runs from run_000001. Two
managers rooted at different projects therefore produced the same pipe name,
and the second one to start silently fell back to the file transport.

Run this after any change to default_channel_address. COLLIDE must be False.
"""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from genericagent.agents.realtime_ipc import default_channel_address  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        addr_a = default_channel_address(Path(a) / "channels", "run_000001")
        addr_b = default_channel_address(Path(b) / "channels", "run_000001")
        print("root A channel address:", addr_a)
        print("root B channel address:", addr_b)
        collide = str(addr_a) == str(addr_b)
        print("COLLIDE:", collide)
        return 1 if collide else 0


if __name__ == "__main__":
    raise SystemExit(main())
