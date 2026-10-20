"""Stop hook: beep once after a complete GenericAgent user turn."""

import sys


def main() -> None:
    try:
        sys.stdin.buffer.read()
    except Exception:
        pass
    try:
        import winsound

        winsound.Beep(800, 300)
    except Exception:
        try:
            sys.stderr.write("\a")
            sys.stderr.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
