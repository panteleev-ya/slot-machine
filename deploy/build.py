#!/usr/bin/env python3
"""Build the single-file executable: src/slot_machine zipped as a Python zipapp.

    python3 deploy/build.py [OUTPUT]      (default: dist/slot-machine)

Standard library only. The result runs anywhere with Python 3.11+ on the PATH,
which is how the Pi runs it: no pip, no venv, nothing installed system-wide.
"""

from __future__ import annotations

import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "slot_machine"


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)

    def keep(path: Path) -> bool:  # path is relative to src/
        return "__pycache__" not in path.parts and path.suffix != ".pyc"

    zipapp.create_archive(
        ROOT / "src",
        target=output,
        interpreter="/usr/bin/env python3",
        main="slot_machine.cli:main",
        filter=lambda p: keep(p) and p.parts[0] == "slot_machine",
        compressed=True,
    )
    return output


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "slot-machine")
    print(f"built {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out} ({out.stat().st_size} bytes)")
