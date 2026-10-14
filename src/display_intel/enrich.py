"""規格補完 agent：針對缺少重點規格的新機型，主動搜尋官方規格頁與評測再萃取一次。

流程：
1. 挑選近期、非傳聞、重點欄位缺 enrich_min_missing 項以上、近幾天沒補查過的機型
2. 每款用 1 次 Tavily 搜尋「品牌 機型 display specs」，只保留真的提到這款機型的頁面
3. AI 只萃取這款機型的螢幕規格（focus 模式），一樣經過原文比對
4. 合併回資料庫；有新欄位就會在週報「規格更新」出現
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable
from urllib.parse import urlsplit

import httpx

from . import verify
from .analytics import completeness, missing_fields
from .collect import UA, canonical_url
from .config import env
from .db import DB, utcnow
from .extract import extract_all
from .llm import LLM
from .schema import Confidence, PanelRecord, RawArticle

log = logging.getLogger(__name__)

# 品牌官方網域（來自這些網域的頁面視為官方，可信度 A）
BRAND_DOMAINS = {
    "asus": ["asus.com"], "lenovo": ["lenovo.com"], "hp": ["hp.com"], "dell": ["dell.com"],
    "alienware": ["dell.com", "alienware.com"], "acer": ["acer.com"], "msi": ["msi.com"],
    "apple": ["apple.com"], "samsung": ["samsung.com"], "microsoft": ["microsoft.com"],
    "razer": ["razer.com"], "lg": ["lg.com"], "gigabyte": ["gigabyte.com"], "framework": ["frame.work"],
    "huawei": ["huawei.com", "consumer.huawei.com"], "xiaomi": ["mi.com", "xiaomi.com"],
}
REVIEW_HINTS = ("notebookcheck", "review", "rtings", "laptopmedia", "tomshardware", "pcmag", "評測", "评测", "レビュー")


def source_type_for(url: str, title: str, brand: str | None) -> str:
    host = urlsplit(url).netloc.lower()
    b = (brand or "").lower().split()[0] if brand else ""
    if any(host.endswith(d) for d in BRAND_DOMAINS.get(b, [])):
        return "official"
    hay = (url + " " + title).lower()
    if any(h in hay for h in REVIEW_HINTS):
        return "review"
    return "media"


def candidates(db: DB, col: dict, now=None) -> list[PanelRecord]:
    now = now or utcnow()
    within = timedelta(days=col.get("enrich_within_days", 45))
    retry = timedelta(days=col.get("enrich_retry_days", 6))
    out = []
    for r in db.all_panels():
        if r.confidence.rank < Confidence.C.rank:  # 傳聞通常查不到規格頁
            continue
        if r.first_seen < now - within:
            continue
        if r.enrich_attempts >= col.get("enrich_max_attempts", 3):
            continue
        if r.last_enriched and r.last_enriched > now - retry:
            continue
        if len(missing_fields(r)) < col.get("enrich_min_missing", 4):
            continue
        out.append(r)
    out.sort(key=lambda r: (-len(missing_fields(r)), -r.first_seen.timestamp()))
    return out


def search_specs(client: httpx.Client, rec: PanelRecord, max_results: int = 6) -> list[RawArticle]:
    """搜尋這款機型的規格頁與評測，只保留內文真的提到這款機型的結果。"""
    key = env("TAVILY_API_KEY")
    if not key:
        return []
    name = f"{rec.spec.brand} {rec.spec.model}".strip()
    r = client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {key}"},
                    json={"query": f"{name} display panel specs review", "topic": "general",
                          "search_depth": "basic", "max_results": max_results, "include_raw_content": "text"})
    r.raise_for_status()
    target = verify.model_norm(rec.spec)
    out = []
    for item in r.json().get("results", []):
        raw = item.get("raw_content") or ""
        text = f"{item.get('title', '')} {item.get('content', '')} {raw[:20000]}"
        if not target or target not in verify._norm(text):
            continue
        out.append(RawArticle(
            url=canonical_url(item["url"]), title=item.get("title", ""), source=urlsplit(item["url"]).netloc,
            source_type=source_type_for(item["url"], item.get("title", ""), rec.spec.brand), lang="en",
            summary=item.get("content", "")[:2000], content=raw, content_level="full" if len(raw) > 800 else "snippet"))
    # 官方與評測優先
    out.sort(key=lambda a: {"official": 0, "review": 1}.get(a.source_type, 2))
    return out[:3]


def enrich(db: DB, cfg: dict, llm: LLM, limit: int, should_stop: Callable[[], bool] = lambda: False) -> dict:
    """補完最多 limit 款機型。回傳統計。"""
    col = cfg.get("collection") or {}
    stats = {"candidates": 0, "searched": 0, "pages": 0, "improved": 0, "fields_added": 0, "stopped": "", "details": []}
    if limit <= 0 or not env("TAVILY_API_KEY"):
        stats["stopped"] = "未設定 TAVILY_API_KEY 或補完數量為 0"
        return stats
    todo = candidates(db, col)
    stats["candidates"] = len(todo)
    seen = db.seen_urls()
    with httpx.Client(timeout=httpx.Timeout(30.0), headers={"User-Agent": UA}) as client:
        for rec in todo[:limit]:
            if should_stop():
                stats["stopped"] = "已到收工時間"
                break
            before = completeness(rec)[0]
            name = f"{rec.spec.brand} {rec.spec.model}"
            rec.enrich_attempts += 1
            rec.last_enriched = utcnow()
            db.put_panel(rec)
            try:
                pages = [p for p in search_specs(client, rec) if p.url not in seen]
            except Exception as exc:
                log.warning("補完搜尋失敗 %s：%s", name, exc)
                continue
            stats["searched"] += 1
            stats["pages"] += len(pages)
            if not pages:
                continue
            results, ex_stats = extract_all(pages, llm, cfg["llm"], should_stop, focus=name)
            target = verify.model_norm(rec.spec)
            kept = []
            for article, ex, flags in results:
                ex.panels = [p for p in ex.panels if verify._norm(p.brand) == verify._norm(rec.spec.brand)
                             and verify.model_norm(p) == target]
                ex.platform_techs, ex.fabs, ex.industry_note = [], [], None
                if ex.panels:
                    kept.append((article, ex, flags))
            verify.ingest(db, kept)
            for p in ex_stats.get("processed", []):
                db.add_article(p.url, p.title, p.source)
                seen.add(p.url)
            after_rec = verify.find_existing(db, rec.spec) or rec
            gained = completeness(after_rec)[0] - before
            if gained > 0:
                stats["improved"] += 1
                stats["fields_added"] += gained
                stats["details"].append(f"{name}：{before} → {before + gained} 項")
            if ex_stats.get("stopped"):  # AI 額度用完或到收工時間
                stats["stopped"] = ex_stats["stopped"]
                break
    log.info("規格補完：候選 %d 款，搜尋 %d 款，%d 款補到新規格，共新增 %d 項",
             stats["candidates"], stats["searched"], stats["improved"], stats["fields_added"])
    return stats
