"""Pure-Python weatherbot model package.

Exports:
    CODE_VERSION   — short string identifying the model code at runtime
    compute        — main entry point: compute(inputs, cfg) -> ModelOutput
    ModelInputs    — input dataclass
    ModelOutput    — output dataclass
    ModelConfig    — configuration dataclass
"""
import hashlib
import subprocess
from pathlib import Path


def _resolve_code_version() -> str:
    pkg_dir = Path(__file__).resolve().parent
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(pkg_dir.parent),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        if sha:
            return sha
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    h = hashlib.sha256()
    for name in ("primitives.py", "compute.py", "config.py", "types.py"):
        path = pkg_dir / name
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()[:7]


CODE_VERSION: str = _resolve_code_version()

from .config import ModelConfig, TodayMaxMode
from .types import ModelInputs, ModelOutput
from .compute import compute

__all__ = ["CODE_VERSION", "ModelConfig", "ModelInputs", "ModelOutput", "TodayMaxMode", "compute"]
