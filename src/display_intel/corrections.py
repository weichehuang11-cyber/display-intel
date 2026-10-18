"""人工更正：config.yaml 的 corrections 清單，最高優先，網路資料不能覆蓋。

每次蒐集與每次產生週報前都會套用（重複套用不會重複產生更新事件）。
- 數值有實際改變時產生 updated 事件（tag="manual"），週報「規格更新」會列出來確認已生效
- 設成 null（空白）＝確認這個數值是錯的，保持空白，之後的新聞也不能再填
- 找不到對應機型的更正，健康檢查會提醒
"""

from __future__ import annotations

import logging

from .db import DB, utcnow
from .panel_decode import canonical_maker
from .schema import Confidence, FieldSource, PanelEvent, PanelRecord, PanelSpec

log = logging.getLogger(__name__)

IDENTITY = {"brand", "model"}


def items(cfg: dict) -> list[dict]:
    return [c for c in (cfg.get("corrections") or []) if isinstance(c, dict) and c.get("brand") and c.get("model")]


def _label(c: dict) -> str:
    extra = "、".join(f"{k}={v}" for k, v in (c.get("match") or {}).items())
    return f"{c['brand']} {c['model']}" + (f"（{extra}）" if extra else "")


def matches(c: dict, rec: PanelRecord) -> bool:
    from .verify import model_norm
    s = rec.spec
    if (s.brand or "").strip().lower() != str(c["brand"]).strip().lower():
        return False
    if model_norm(s) != model_norm(PanelSpec(brand=str(c["brand"]), model=str(c["model"]))):
        return False
    for k, v in (c.get("match") or {}).items():
        cur = getattr(s, k, None)
        if cur is None or (str(cur).lower() != str(v).lower() and not (isinstance(cur, (int, float)) and _num(v) == cur)):
            return False
    return True


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def validate(c: dict) -> tuple[dict, list[str]]:
    """檢查欄位名稱與型別；回傳（可套用的值, 錯誤說明）。"""
    errors, values = [], {}
    for f, v in (c.get("set") or {}).items():
        if f not in PanelSpec.model_fields or f in IDENTITY or f == "evidence":
            errors.append(f"{_label(c)}：沒有「{f}」這個欄位")
            continue
        if v is None:
            values[f] = None
            continue
        try:
            values[f] = getattr(PanelSpec(**{f: v}), f)
        except Exception:
            errors.append(f"{_label(c)}：{f} 的值「{v}」格式不對")
            continue
        if f == "panel_maker":
            values[f] = canonical_maker(values[f]) or values[f]
    return values, errors


def apply(db: DB, cfg: dict) -> dict:
    """套用全部人工更正。回傳統計（matched / changed / unmatched / errors）。"""
    from .verify import _derived
    stats = {"items": 0, "matched": 0, "changed": 0, "unmatched": [], "errors": []}
    corr = items(cfg)
    if not corr:
        return stats
    records = db.all_panels()
    now = utcnow()
    for c in corr:
        stats["items"] += 1
        values, errors = validate(c)
        stats["errors"] += errors
        targets = [r for r in records if matches(c, r)]
        if not targets:
            stats["unmatched"].append(_label(c))
            continue
        note = str(c.get("note") or "").strip()
        for rec in targets:
            stats["matched"] += 1
            data = rec.spec.model_dump()
            changes, touched = {}, False
            for f, v in values.items():
                if data.get(f) != v:
                    changes[f] = [data.get(f), v]
                    data[f] = v
                fs = rec.field_sources.get(f)
                if fs is None or fs.kind != "manual" or fs.source != note or f in changes:
                    touched = True
                    rec.field_sources[f] = FieldSource(source=note, conf=Confidence.A, at=now, kind="manual")
                    rec.corroborated.pop(f, None)
                    if v is None:
                        data["evidence"].pop(f, None)
                    else:
                        data["evidence"][f] = "人工更正" + (f"：{note}" if note else "")
            if not touched:
                continue  # 已套用過，什麼都不用做
            rec.spec = PanelSpec(**data)
            if changes:
                rec.last_updated = now
                rec.flags = [fl for fl in rec.flags if not any(f"：{f}（" in fl for f in changes)]  # 已人工裁決的不一致不再列
            rec = _derived(rec)
            db.put_panel(rec)
            if changes:
                stats["changed"] += 1
                ev = PanelEvent(kind="updated", record=rec, changes=changes)
                payload = ev.model_dump(mode="json") | {"tag": "manual"}
                db.add_event("panel", "updated", payload, Confidence.A.value, "", "人工更正" + (f"：{note}" if note else ""))
    if stats["changed"] or stats["unmatched"] or stats["errors"]:
        log.info("人工更正：%d 項，套用到 %d 筆，改變 %d 筆；找不到 %s；錯誤 %s", stats["items"], stats["matched"],
                 stats["changed"], stats["unmatched"] or "無", stats["errors"] or "無")
    return stats


def unmatched(records: list[PanelRecord], cfg: dict) -> tuple[list[str], list[str]]:
    """健康檢查用：找不到機型的更正、格式錯誤。"""
    miss, errs = [], []
    for c in items(cfg):
        errs += validate(c)[1]
        if not any(matches(c, r) for r in records):
            miss.append(_label(c))
    return miss, errs
