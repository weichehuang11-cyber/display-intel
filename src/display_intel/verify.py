"""查核層：去重、可信度、合併到資料庫、傳聞追蹤。"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from . import derive
from .db import DB, utcnow
from .schema import (SOURCE_TYPE_CONFIDENCE, Confidence, Extraction, PanelEvent,
                     PanelRecord, PanelSpec, RawArticle)

SPEC_FIELDS = [f for f in PanelSpec.model_fields if f != "evidence"]


def _norm(s) -> str:
    return re.sub(r"[\s\-_()（）®™]+", "", str(s or "")).lower()


def tech_family(tech: str | None) -> str:
    t = (tech or "").lower()
    if "oled" in t:
        return "oled"
    if t:
        return "lcd"
    return "?"


def panel_key(spec: PanelSpec) -> str:
    """同品牌＋同機型＋同面板選項 = 同一筆紀錄。"""
    size = f"{spec.size_inch:g}" if spec.size_inch else "?"
    res = f"{spec.res_w}x{spec.res_h}" if spec.res_w and spec.res_h else "?"
    return "|".join([_norm(spec.brand), _norm(spec.model), size, tech_family(spec.panel_tech), res])


def confidence_for(article: RawArticle, is_rumor: bool) -> Confidence:
    base = SOURCE_TYPE_CONFIDENCE.get(article.source_type, Confidence.C)
    if is_rumor and base.rank > Confidence.D.rank:
        return Confidence.D
    return base


def dedupe_articles(articles: list[RawArticle], db: DB, threshold: float = 0.85) -> list[RawArticle]:
    """移除已處理過的網址，以及標題幾乎相同的轉載文章（保留可信度較高的來源）。"""
    seen_urls = db.seen_urls()
    rank = {"official": 5, "review": 4, "media": 3, "aggregator": 2, "social": 1}
    candidates = sorted((a for a in articles if a.url not in seen_urls),
                        key=lambda a: (-rank.get(a.source_type, 0), -(len(a.content))))
    kept: list[RawArticle] = []
    titles = [_norm(t) for t in db.recent_titles()]
    for a in candidates:
        t = _norm(a.title)
        if any(SequenceMatcher(None, t, o).ratio() >= threshold for o in titles):
            continue
        kept.append(a)
        titles.append(t)
    return kept


def _derived(rec: PanelRecord) -> PanelRecord:
    s = rec.spec
    rec.ppi = derive.ppi(s.res_w, s.res_h, s.size_inch)
    rec.pixel_count = s.res_w * s.res_h if s.res_w and s.res_h else None
    rec.aspect_ratio = derive.aspect_ratio(s.res_w, s.res_h)
    rec.density_class = derive.density_class(rec.ppi)
    return rec


def merge_panel(db: DB, spec: PanelSpec, conf: Confidence, article: RawArticle, flags: list[str]) -> PanelEvent | None:
    key = panel_key(spec)
    now = utcnow()
    existing = db.get_panel(key)
    if existing is None:
        rec = _derived(PanelRecord(key=key, spec=spec, confidence=conf, source_url=article.url,
                                   source_name=article.source, first_seen=now, last_updated=now, flags=flags))
        db.put_panel(rec)
        return PanelEvent(kind="new", record=rec)

    old = existing.spec.model_dump()
    new = spec.model_dump()
    changes: dict[str, list] = {}
    for f in SPEC_FIELDS:
        nv, ov = new.get(f), old.get(f)
        if nv in (None, [], ""):
            continue
        if ov in (None, [], ""):
            old[f] = nv
            changes[f] = [None, nv]
        elif nv != ov:
            if conf.rank > existing.confidence.rank:
                old[f] = nv
                changes[f] = [ov, nv]
            elif conf.rank == existing.confidence.rank:
                existing.flags.append(f"來源數值不一致：{f}（{ov} vs {nv}，{article.source}）")
    old["evidence"] = {**existing.spec.evidence, **{k: v for k, v in spec.evidence.items() if k in changes}}

    rumor_confirmed = existing.confidence == Confidence.D and conf.rank >= Confidence.C.rank
    if not changes and not rumor_confirmed:
        db.put_panel(existing)  # 保留新的衝突標記
        return None

    if conf.rank > existing.confidence.rank:
        existing.confidence = conf
        existing.source_url, existing.source_name = article.url, article.source
    existing.spec = PanelSpec(**old)
    existing.last_updated = now
    existing.flags = list(dict.fromkeys(existing.flags + flags))[-20:]
    rec = _derived(existing)
    db.put_panel(rec)
    return PanelEvent(kind="rumor_confirmed" if rumor_confirmed else "updated", record=rec, changes=changes)


def _hash(*parts) -> str:
    return hashlib.sha1("|".join(_norm(p) for p in parts).encode()).hexdigest()[:16]


def ingest(db: DB, results: list[tuple[RawArticle, Extraction, list[str]]]) -> dict:
    """把萃取結果寫入資料庫並產生報告事件。回傳統計。"""
    stats = {"panel_new": 0, "panel_updated": 0, "rumor_confirmed": 0, "platform": 0, "fab": 0, "industry": 0}
    for article, ex, flags in results:
        conf = confidence_for(article, ex.is_rumor)
        for spec in ex.panels:
            ev = merge_panel(db, spec, conf, article, flags)
            if ev and ev.record.confidence != Confidence.E:
                db.add_event("panel", ev.kind, ev.model_dump(mode="json"), conf.value, article.url, article.source)
                stats["panel_" + ev.kind if ev.kind != "rumor_confirmed" else "rumor_confirmed"] += 1
        for t in ex.platform_techs:
            key = _hash(t.vendor, t.tech_name, t.event_type)
            if not db.has_recent_event(key):
                db.add_event("platform", t.event_type, t.model_dump(mode="json"), conf.value, article.url, article.source, key)
                stats["platform"] += 1
        for f in ex.fabs:
            key = _hash(f.maker, f.generation, f.technology, f.status)  # 同廠同世代同狀態視為同一則（廠區名稱各家報導寫法不一）
            if not db.has_recent_event(key):
                db.add_event("fab", f.status or "other", f.model_dump(mode="json"), conf.value, article.url, article.source, key)
                stats["fab"] += 1
        if ex.industry_note:
            key = _hash(article.title)
            if not db.has_recent_event(key):
                db.add_event("industry", "note", {"note": ex.industry_note, "title": article.title},
                             conf.value, article.url, article.source, key)
                stats["industry"] += 1
    return stats
