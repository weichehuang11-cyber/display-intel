"""回溯機制：補建過去幾季的新機資料，讓季度趨勢可以比較。

- 以「季度 × 查詢」為單位排程，做過的不重做（記在 backfill_state 表）
- Tavily 以日期區間搜尋（start_date / end_date）；Google 新聞用 after:/before: 搜尋（免費，只有摘要）
- 預設 config.yaml 的 backfill.start_on（11/1）之後才自動執行，每次最多用 tavily_per_run 次，
  每月最多 monthly_tavily_budget 次，並受全體 Tavily 月上限保護
- 回溯到的資料標記 tag="backfill"：不會出現在週報的「新機型」，只進季度趨勢與 Excel
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote_plus, urlsplit

import httpx

from . import verify
from .collect import UA, _rss_source, canonical_url, classify
from .config import env
from .db import DB
from .extract import extract_all
from .llm import LLM
from .schema import RawArticle

log = logging.getLogger(__name__)


def quarter_window(q: str) -> tuple[date, date]:
    """'2026 Q1' → (2026-01-01, 2026-03-31)"""
    y, n = q.replace(" ", "").upper().split("Q")
    start = date(int(y), (int(n) - 1) * 3 + 1, 1)
    end_month = start.month + 3
    end = (date(start.year + (end_month > 12), (end_month - 1) % 12 + 1, 1)) - timedelta(days=1)
    return start, end


def units(cfg: dict) -> list[dict]:
    """所有回溯工作單位（依 config 的季度順序）。"""
    bf = cfg.get("backfill") or {}
    events = {int(k): v for k, v in (bf.get("events") or {}).items()}
    out = []
    for q in bf.get("quarters", []):
        qn = int(q.upper().split("Q")[1])
        event = events.get(qn, "")
        for t in bf.get("tavily_queries", []):
            if "{event}" in t and not event:
                continue
            out.append({"id": f"tv|{q}|{t}", "quarter": q, "kind": "tavily", "query": t.replace("{event}", event).strip()})
        for g in bf.get("google_news_queries", []):
            out.append({"id": f"gn|{q}|{g['q']}|{g.get('lang', 'en')}", "quarter": q, "kind": "gnews",
                        "query": g["q"], "loc": g})
    return out


def progress(db: DB, cfg: dict) -> dict:
    us = units(cfg)
    done = db.backfill_done()
    by_q: dict[str, list] = {}
    for u in us:
        by_q.setdefault(u["quarter"], []).append(u["id"] in done)
    return {"quarters": {q: f"{sum(v)}/{len(v)}" for q, v in by_q.items()},
            "done": sum(1 for u in us if u["id"] in done), "total": len(us)}


def due(cfg: dict, today: date | None = None) -> bool:
    bf = cfg.get("backfill") or {}
    if not bf.get("enabled"):
        return False
    start = bf.get("start_on")
    today = today or datetime.now(timezone.utc).date()
    return not start or today >= date.fromisoformat(str(start))


def _published(item: dict, start: date, end: date) -> datetime:
    """Tavily 回傳的發布日期；沒有或不在這一季時，用季中代表（搜尋已限定在這一季）。"""
    from email.utils import parsedate_to_datetime
    raw = str(item.get("published_date") or "").strip()
    d = None
    try:
        d = datetime.fromisoformat(raw[:10]) if raw[:4].isdigit() else parsedate_to_datetime(raw)
    except (ValueError, TypeError):
        d = None
    if d and start <= d.date() <= end:
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
    mid = start + (end - start) / 2
    return datetime(mid.year, mid.month, mid.day, tzinfo=timezone.utc)


def _tavily_unit(client: httpx.Client, u: dict, max_results: int) -> list[RawArticle]:
    start, end = quarter_window(u["quarter"])
    r = client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {env('TAVILY_API_KEY')}"},
                    json={"query": u["query"], "topic": "general", "search_depth": "basic", "max_results": max_results,
                          "start_date": start.isoformat(), "end_date": end.isoformat(), "include_raw_content": "text"})
    r.raise_for_status()
    out = []
    for item in r.json().get("results", []):
        raw = item.get("raw_content") or ""
        out.append(RawArticle(url=canonical_url(item["url"]), title=item.get("title", ""), source=urlsplit(item["url"]).netloc,
                              source_type="media", lang="en", published=_published(item, start, end),
                              summary=item.get("content", "")[:2000], content=raw,
                              content_level="full" if len(raw) > 800 else "snippet"))
    return out


def _gnews_unit(client: httpx.Client, u: dict) -> list[RawArticle]:
    start, end = quarter_window(u["quarter"])
    loc = u["loc"]
    q = f"{u['query']} after:{start.isoformat()} before:{(end + timedelta(days=1)).isoformat()}"
    url = (f"https://news.google.com/rss/search?q={quote_plus(q)}&hl={loc.get('hl', 'en-US')}"
           f"&gl={loc.get('gl', 'US')}&ceid={loc.get('ceid', 'US:en')}")
    src = {"name": f"Google 新聞回溯（{loc.get('lang', 'en')}）", "url": url, "type": "aggregator", "lang": loc.get("lang", "en")}
    since = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    return [a for a in _rss_source(client, src, since) if not a.published or a.published.date() <= end]


def run(db: DB, cfg: dict, llm: LLM, should_stop: Callable[[], bool] = lambda: False, force: bool = False) -> dict:
    """執行一輪回溯。force=True 時不看開始日期（手動執行）。"""
    bf = cfg.get("backfill") or {}
    col = cfg.get("collection") or {}
    stats = {"units": 0, "tavily": 0, "articles": 0, "ingest": {}, "stopped": "", "progress": {}}
    if not force and not due(cfg):
        stats["stopped"] = f"尚未到開始日期（{bf.get('start_on')}）" if bf.get("enabled") else "回溯未啟用"
        return stats
    done = db.backfill_done()
    pending = [u for u in units(cfg) if u["id"] not in done]
    if not pending:
        stats["stopped"] = "回溯已全部完成"
        stats["progress"] = progress(db, cfg)
        return stats
    hard_cap = col.get("tavily_monthly_hard_cap", 950)
    month_budget = bf.get("monthly_tavily_budget", 160)
    tv_left = min(bf.get("tavily_per_run", 12), month_budget - db.usage("tavily_backfill"), hard_cap - db.usage("tavily"))
    has_key = bool(env("TAVILY_API_KEY"))
    chosen = []
    for u in pending:
        if u["kind"] == "tavily":
            if not has_key or tv_left <= 0:
                continue
            tv_left -= 1
        chosen.append(u)
        if len(chosen) >= bf.get("units_per_run", 30):
            break
    if not chosen:
        stats["stopped"] = "本月回溯的 Tavily 額度已用完"
        return stats

    articles: list[RawArticle] = []
    finished = []
    with httpx.Client(timeout=httpx.Timeout(40.0), headers={"User-Agent": UA}, follow_redirects=True) as client:
        for u in chosen:
            try:
                if u["kind"] == "tavily":
                    got = _tavily_unit(client, u, bf.get("max_results", 15))
                    db.add_usage("tavily", 1)
                    db.add_usage("tavily_backfill", 1)
                    stats["tavily"] += 1
                else:
                    got = _gnews_unit(client, u)
            except Exception as exc:
                log.warning("回溯搜尋失敗 %s：%s", u["id"], exc)
                continue
            for a in got:
                a.topics = classify(a, cfg["keywords"])
            articles.extend(a for a in got if a.topics)
            finished.append((u, len(got)))
    stats["units"] = len(finished)
    articles = verify.dedupe_articles(articles, db)
    stats["articles"] = len(articles)
    rank = {"official": 0, "review": 1, "media": 2, "aggregator": 3, "social": 4}
    articles.sort(key=lambda a: (rank.get(a.source_type, 5), a.content_level != "full"))
    results, ex_stats = extract_all(articles, llm, cfg["llm"], should_stop)
    processed = ex_stats.pop("processed")
    stats["ingest"] = verify.ingest(db, results, tag="backfill")
    for a in processed:
        db.add_article(a.url, a.title, a.source)
    # 文章都處理完（或沒有文章）的單位才標記完成；AI 額度中途用完時，沒處理到的單位下次再做
    if not ex_stats.get("stopped") or len(processed) == len(articles):
        for u, n in finished:
            db.mark_backfill_done(u["id"], n)
    else:
        stats["stopped"] = ex_stats["stopped"] + "（未完成的部分下次繼續）"
        for u, n in finished:
            if u["kind"] == "tavily":  # Tavily 已經花了額度，結果的網址已記錄，標記完成避免重複扣款
                db.mark_backfill_done(u["id"], n)
    stats["progress"] = progress(db, cfg)
    log.info("回溯：完成 %d 個單位（Tavily %d 次），文章 %d 篇，%s；進度 %s",
             stats["units"], stats["tavily"], stats["articles"], stats["ingest"], stats["progress"]["quarters"])
    return stats
