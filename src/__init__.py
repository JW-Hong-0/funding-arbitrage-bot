import sys
from pathlib import Path

# Ensure shared library is importable when running via `python -m src.*`.
root = Path(__file__).resolve().parents[1]
libs_dir = root.parent.parent / "libs"
shared_lib = libs_dir / "shared_crypto_lib"
if shared_lib.exists():
    sys.path.insert(0, str(libs_dir))
