"""`python -m signin`. Mac Chrome on Darwin; Linux Chrome otherwise."""

from __future__ import annotations

import sys

from signin.mac import ProbeError


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    linux = sys.platform != "darwin" or "--linux" in args or "--check" in args
    if "--linux" in args:
        args = [arg for arg in args if arg != "--linux"]
    if linux:
        from signin.linux import main as run
    else:
        from signin.mac import main as run
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        if sys.platform == "darwin":
            print("\nStopped. The Chrome window is left as it is.", file=sys.stderr)
        else:
            print("\nStopped. The container exits and Chrome stops with it.", file=sys.stderr)
        sys.exit(130)
