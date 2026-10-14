"""讀取 config.yaml 與環境變數。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """簡易 .env 讀取（本機開發用；GitHub Actions 用 Secrets）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def load_config(path: str | Path | None = None) -> dict:
    load_dotenv()
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    report = cfg.setdefault("report", {})
    report["output_dir"] = str(ROOT / report.get("output_dir", "output"))
    report["database"] = str(ROOT / report.get("database", "data/intel.sqlite"))
    return cfg


def env(name: str, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"缺少環境變數 {name}，請在 .env 或 GitHub Secrets 設定")
    return value
