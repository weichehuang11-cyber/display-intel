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
    cfg = _load_config(path)
    from .panel_decode import configure_res_codes
    configure_res_codes(cfg)
    return cfg


def _load_config(path: str | Path | None = None) -> dict:
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


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _rotate(items: list, n: int, offset: int) -> list:
    """從清單中輪流取 n 個（每天換一組，一週內所有查詢都會用到）。"""
    if not items or n <= 0:
        return []
    if n >= len(items):
        return list(items)
    start = (offset * n) % len(items)
    return [items[(start + i) % len(items)] for i in range(n)]


def apply_profile(cfg: dict, now=None, profile: str | None = None) -> dict:
    """決定這次執行用平日輕量（light）或週日完整（full）設定，結果放在 cfg["run"]。

    profile 未指定時：collection.full_day 那天用 full，enrich_day 那天只做規格補完，其他天用 light。
    展會月（collection.event_months）會額外加查詢。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(cfg.get("schedule", {}).get("timezone", "Asia/Taipei"))
    now = (now or datetime.now(tz)).astimezone(tz)
    col = cfg.get("collection") or {}
    if profile in (None, "", "auto"):
        full_day = WEEKDAYS.index(str(col.get("full_day", "sunday")).lower())
        enrich_day = col.get("enrich_day")
        if now.weekday() == full_day:
            profile = "full"
        elif enrich_day and now.weekday() == WEEKDAYS.index(str(enrich_day).lower()):
            profile = "enrich"
        else:
            profile = "light"
    p = dict(col.get(profile) or {})
    tv = cfg.get("sources", {}).get("tavily") or {}
    day = now.date().toordinal()
    queries = tv.get("queries", [])
    n = p.get("tavily_queries_per_run", tv.get("max_queries_per_run", 20))
    if profile in ("enrich", "backfill"):
        chosen = []  # 只做規格補完或回溯，不搜尋本週新聞
    elif profile == "full":
        chosen = list(queries)[:n]
    else:
        chosen = _rotate(queries, n, day)
    event = now.month in col.get("event_months", [])
    if event and profile not in ("enrich", "backfill"):
        chosen += _rotate(tv.get("event_queries", []), col.get("event_extra_queries_per_run", 0), day)
    weekly = cfg.get("schedule", {}).get("cadence", "daily") == "weekly"
    cfg["run"] = {
        "profile": profile,
        "event_month": event,
        "lookback_hours": p.get("lookback_hours", cfg.get("schedule", {}).get("lookback_hours", 170 if weekly else 26)),
        "news_window": p.get("news_window", "7d" if weekly else "1d"),
        "tavily_time_range": p.get("tavily_time_range", "week" if weekly else "day"),
        "tavily_days": p.get("tavily_days"),
        "tavily_queries": chosen,
        "max_ai_calls": p.get("max_ai_calls", cfg.get("llm", {}).get("max_calls_per_run", 40)),
        "enrich_models": p.get("enrich_models", 0),
        "extract_model": p.get("extract_model"),
        "collect_news": profile not in ("enrich", "backfill"),
    }
    return cfg
