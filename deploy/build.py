#!/usr/bin/env python3
"""Build the single-file executable: src/slot_machine zipped as a Python zipapp.

    python3 deploy/build.py [OUTPUT]      (default: dist/slot-machine)

Standard library only. The result runs anywhere with Python 3.11+ on the PATH,
which is how the Pi runs it: no pip, no venv, nothing installed system-wide.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "slot_machine"

# zipapp's own `main=` entry point ignores the function's return value, so every
# run would exit 0. Ship an entry point that passes the exit code on to cron.
ENTRY_POINT = "import sys\n\nfrom slot_machine.cli import main\n\nsys.exit(main())\n"


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        shutil.copytree(
            PACKAGE, stage / "slot_machine", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        (stage / "__main__.py").write_text(ENTRY_POINT)
        zipapp.create_archive(stage, target=output, interpreter="/usr/bin/env python3", compressed=True)
    return output


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "slot-machine")
    print(f"built {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out} ({out.stat().st_size} bytes)")
