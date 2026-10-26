"""系統健康與資料查核：每週報告最後一段，全部由程式計算（不花 AI 與 Tavily 額度）。

兩部分：
1. 運作健康：排程有沒有照跑、來源是否失效、AI／Tavily 額度、萃取錯誤率、資料量是否異常
2. 資料查核：原文依據覆蓋、AI 欄位被刪比例、多來源佐證、來源數值不一致、跨欄位矛盾、可能重複的機型、抽樣稽核

每個檢查回傳 level：ok（正常）／warn（注意）／bad（需處理），報告開頭顯示總燈號。
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from .db import DB
from .panel_decode import canonical_maker, decode
from .schema import Confidence, PanelRecord

LEVEL_ICON = {"ok": "🟢", "warn": "🟡", "bad": "🔴"}
LEVEL_RANK = {"ok": 0, "warn": 1, "bad": 2}
# 不需要逐字原文依據的欄位（識別用或程式計算）
_NO_EVIDENCE = {"brand", "model", "product_type", "regions", "evidence", "display_marketing_name"}
# 來源不一致只看螢幕規格；處理器、顯卡、機型名稱不同通常是不同配置或寫法差異，不算矛盾
_CONFLICT_SKIP = {"brand", "model", "cpu_platform", "gpu", "gpu_switching", "platform_certifications",
                  "display_power_tech", "launch_period", "display_marketing_name", "product_type", "regions",
                  "price", "price_currency", "form_factor"}  # 價格因配置與地區而異；form_factor 是描述性欄位
FIELD_NAMES = {
    "size_inch": "尺寸", "res_w": "解析度", "res_h": "解析度", "refresh_hz": "刷新率", "response_ms": "反應時間",
    "sdr_nits": "SDR 亮度", "hdr_peak_nits": "HDR 峰值", "dci_p3_pct": "DCI-P3", "srgb_pct": "sRGB",
    "panel_tech": "面板技術", "color_depth_bit": "色深", "stylus": "觸控筆", "panel_maker": "面板廠", "panel_model": "面板型號", "surface": "表面處理", "touch": "觸控",
}


def _check(name: str, level: str, detail: str, items: list | None = None) -> dict:
    return {"name": name, "level": level, "icon": LEVEL_ICON[level], "detail": detail, "notes": items or []}


def _runs(db: DB, since: datetime) -> list[dict]:
    out = []
    for rid, stage, started, stats in db.conn.execute("SELECT id, stage, started, stats FROM runs ORDER BY id"):
        try:
            t = datetime.fromisoformat(started)
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t >= since:
            out.append({"id": rid, "stage": stage, "started": t, "stats": json.loads(stats or "{}")})
    return out


# ---------- 1. 運作健康 ----------

def operations(db: DB, cfg: dict, now: datetime, days: int = 7) -> list[dict]:
    hc = cfg.get("health") or {}
    tz = now.tzinfo or timezone.utc
    since = now - timedelta(days=days)
    runs = [r for r in _runs(db, since) if r["stage"] == "collect"]
    checks = []

    # 排程：預期的蒐集日（台灣時間，0=週一）
    weekdays = set(hc.get("collect_weekdays", [0, 2, 4, 6]))
    expected = sorted({(now - timedelta(days=i)).date() for i in range(days)
                       if (now - timedelta(days=i)).weekday() in weekdays})
    ran = {r["started"].astimezone(tz).date() for r in runs}
    first = [r["started"].astimezone(tz).date() for r in _runs(db, datetime(2000, 1, 1, tzinfo=timezone.utc)) if r["stage"] == "collect"]
    expected = [d for d in expected if first and d >= min(first)]
    missing = [d for d in expected if d not in ran and d != now.date()]
    if missing:
        checks.append(_check("排程執行", "bad" if len(missing) >= 2 else "warn",
                             f"應執行 {len(expected)} 次蒐集，缺 {len(missing)} 次",
                             [f"{d.strftime('%m/%d')}（週{'一二三四五六日'[d.weekday()]}）沒有執行紀錄" for d in missing]))
    else:
        checks.append(_check("排程執行", "ok", f"蒐集 {len(runs)} 次，都有照排程執行"))

    # 來源：本期每次都失敗或都是 0 篇
    news_runs = [r for r in runs if r["stats"].get("sources")]  # 週二規格補完日不抓新聞，不列入
    if news_runs:
        err_count: Counter = Counter()
        zero_count: Counter = Counter()
        for r in news_runs:
            for name in (r["stats"].get("source_errors") or {}):
                err_count[name] += 1
            for name, n in (r["stats"].get("sources") or {}).items():
                if not n:
                    zero_count[name] += 1
        dead = sorted({n for n, c in err_count.items() if c == len(news_runs)})
        empty = sorted({n for n, c in zero_count.items() if c == len(news_runs)} - set(dead))
        items = [f"{n}：每次都連線失敗" for n in dead] + [f"{n}：每次都 0 篇" for n in empty]
        checks.append(_check("新聞來源", "warn" if items else "ok",
                             f"{len(items)} 個來源本期沒有貢獻資料" if items else "所有來源都有資料", items))

    # AI 萃取：額度用完、批次失敗
    stopped, failed, total = [], 0, 0
    for r in runs:
        ex = r["stats"].get("extract") or {}
        total += ex.get("batches", 0)
        failed += ex.get("failed_batches", 0)
        if ex.get("stopped"):
            stopped.append(f"{r['started'].astimezone(tz).strftime('%m/%d')} {r['stats'].get('profile', '')}："
                           f"{ex.get('done_batches', 0)}/{ex.get('batches', 0)} 批完成後停止（{ex['stopped']}）")
        en = r["stats"].get("enrich") or {}
        if isinstance(en, dict) and en.get("stopped") and en.get("candidates", 0) > en.get("searched", 0):
            stopped.append(f"{r['started'].astimezone(tz).strftime('%m/%d')} 規格補完：{en.get('candidates')} 款待補只做了 "
                           f"{en.get('searched', 0)} 款（{en['stopped']}）")
        if r["stats"].get("llm_probe") and not r["stats"].get("llm_model"):
            probe = r["stats"]["llm_probe"]
            first = next(iter(probe.values()), "") if isinstance(probe, dict) else ""
            stopped.append(f"{r['started'].astimezone(tz).strftime('%m/%d')}：AI 模型都無法使用，整次沒有萃取"
                           f"（試了 {len(probe)} 個模型，例如：{str(first)[:60]}）")
        for sw in r["stats"].get("llm_switched") or []:
            stopped.append(f"{r['started'].astimezone(tz).strftime('%m/%d')}：額度用完自動換模型 {sw}")
    rate = failed / total if total else 0
    level = "bad" if rate > 0.2 else ("warn" if stopped or rate > 0.05 else "ok")
    checks.append(_check("AI 萃取", level,
                         f"{total} 批、失敗 {failed} 批（{rate:.0%}）" + (f"；{len(stopped)} 次因額度提前停止" if stopped else ""),
                         stopped))

    # Tavily 月用量進度
    col = cfg.get("collection") or {}
    cap = col.get("tavily_monthly_hard_cap", 950)
    used = db.usage("tavily")
    import calendar
    month_frac = now.day / calendar.monthrange(now.year, now.month)[1]
    pace = used / cap if cap else 0
    lvl = "bad" if pace >= 0.95 else ("warn" if pace > month_frac + 0.15 else "ok")
    checks.append(_check("Tavily 額度", lvl, f"本月已用 {used}/{cap}（{pace:.0%}），月份已過 {month_frac:.0%}"
                         + ("，用量超前" if lvl == "warn" else "")))

    # 資料量：本期文章數與前 3 期平均比較
    prev = [r for r in _runs(db, since - timedelta(days=days * 3)) if r["stage"] == "collect" and r["started"] < since]
    cur_n = sum(r["stats"].get("after_dedupe", 0) for r in runs)
    if prev:
        prev_n = sum(r["stats"].get("after_dedupe", 0) for r in prev) / max(1, len({r["started"].isocalendar()[1] for r in prev}))
        ratio = cur_n / prev_n if prev_n else 1
        checks.append(_check("資料量", "warn" if ratio < 0.4 else "ok",
                             f"本期去重後文章 {cur_n} 篇，前幾期平均 {prev_n:.0f} 篇（{ratio:.0%}）"))
    else:
        checks.append(_check("資料量", "ok", f"本期去重後文章 {cur_n} 篇（歷史資料累積中，尚無比較基準）"))
    return checks


# ---------- 2. 資料查核 ----------

def _hdr_level(spec) -> int | None:
    text = " ".join((spec.hdr_formats or []) + (spec.certifications or []))
    m = re.search(r"DisplayHDR\s*(?:True\s*Black\s*)?(\d{3,4})", text, re.I)
    return int(m.group(1)) if m else None


def consistency_issues(rec: PanelRecord) -> list[str]:
    """跨欄位矛盾（數值本身合理，但彼此對不起來）。"""
    s, out = rec.spec, []
    tech = (s.panel_tech or "").upper()
    if "OLED" in tech and (s.mini_led_zones or "MINI" in (s.backlight or "").upper()):
        out.append("面板技術是 OLED，卻有 Mini LED 背光／分區")
    lvl = _hdr_level(s)
    if lvl and s.hdr_peak_nits and s.hdr_peak_nits < lvl * 0.9:
        out.append(f"DisplayHDR {lvl} 認證，但 HDR 峰值只寫 {s.hdr_peak_nits:g} nits")
    if s.vrr_range:
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s.vrr_range)]
        if nums and s.refresh_hz and max(nums) > s.refresh_hz * 1.05:
            out.append(f"VRR 範圍 {s.vrr_range} 超過刷新率 {s.refresh_hz:g}Hz")
    if s.vrr_min_hz and s.refresh_hz and s.vrr_min_hz >= s.refresh_hz:
        out.append(f"VRR 最低 {s.vrr_min_hz:g}Hz 不低於刷新率 {s.refresh_hz:g}Hz")
    dec = decode(s.panel_model)
    if dec and dec.get("size_inch") and s.size_inch and abs(dec["size_inch"] - s.size_inch) > 0.6:
        out.append(f"面板型號 {s.panel_model} 推算為 {dec['size_inch']:g} 吋，與機型 {s.size_inch:g} 吋不符")
    if dec and dec.get("res") and s.res_w and (s.res_w, s.res_h) != dec["res"]:
        out.append(f"面板型號 {s.panel_model} 的解析度代碼推算為 {dec['res'][0]}×{dec['res'][1]}，與報導 {s.res_w}×{s.res_h} 不符")
    if dec and s.panel_maker and canonical_maker(s.panel_maker) != dec["maker"]:
        out.append(f"面板型號推算為 {dec['maker']}，報導寫 {s.panel_maker}")
    if s.touch is False and s.stylus:
        out.append("標示非觸控，卻支援觸控筆")
    if s.size_inch and s.size_inch > 18.5 and (s.product_type or "").lower() not in ("aio", "all-in-one"):
        out.append(f"{s.size_inch:g} 吋超出筆電常見尺寸，產品類型可能是 AIO")
    if s.sdr_nits and s.hdr_peak_nits and s.hdr_peak_nits == s.sdr_nits and lvl and lvl >= 600:
        out.append("SDR 與 HDR 亮度相同，可能把 HDR 峰值當成 SDR")
    return out


_CONFLICT_RE = re.compile(r"來源數值不一致：(\w+)（(.*) vs (.*)，(.*)）$")


def _conflict(rec: PanelRecord, flag: str) -> str | None:
    """把 merge 留下的不一致標記轉成報告文字；非螢幕欄位、寫法詳略不同的不算。"""
    m = _CONFLICT_RE.match(flag)
    if not m or m.group(1) in _CONFLICT_SKIP:
        return None
    f, a, b, src = m.groups()
    if getattr(rec.spec, f, None) in (None, "", []):
        return None  # 這個欄位後來已被移除或清空，舊紀錄不再列出
    a, b = (re.sub(r"(\d)\.0\b", r"\1", x) for x in (a, b))
    na, nb = re.sub(r"\W", "", a.lower()), re.sub(r"\W", "", b.lower())
    if na in nb or nb in na or "其他" in (a, b):
        return None
    if re.findall(r"\d+(?:\.\d+)?", a) == re.findall(r"\d+(?:\.\d+)?", b) and re.search(r"\d", a):
        return None  # 數字都一樣，只是寫法不同（30Hz to 120Hz／30-120Hz）
    return f"{rec.spec.brand} {rec.spec.model}：{FIELD_NAMES.get(f, f)} {a} vs {b}（{src}）"


def _filled(rec: PanelRecord) -> list[str]:
    return [f for f, v in rec.spec.model_dump().items()
            if f not in _NO_EVIDENCE and v not in (None, [], "")]


def _pair_key(a: str, b: str) -> frozenset:
    return frozenset(re.sub(r"\W", "", x.lower()) for x in (a, b))


def possible_duplicates(records: list[PanelRecord], limit: int = 8, ignore: list | None = None) -> list[str]:
    """ignore：config.yaml health.not_duplicates，確認過「不是同一款」的機型名稱對，不再提醒。"""
    from .verify import model_norm
    skip = {_pair_key(*p) for p in (ignore or []) if len(p) == 2}
    by_brand: dict[str, list[PanelRecord]] = {}
    for r in records:
        if r.spec.brand and r.spec.model:
            by_brand.setdefault(r.spec.brand.lower(), []).append(r)
    out, seen = [], set()
    for recs in by_brand.values():
        for i, a in enumerate(recs):
            for b in recs[i + 1:]:
                na, nb = model_norm(a.spec), model_norm(b.spec)
                if _pair_key(a.spec.model, b.spec.model) in skip:
                    continue
                if na == nb or re.findall(r"\d+", na) != re.findall(r"\d+", nb):
                    continue  # 同名不同螢幕選配是正常的；數字不同（P14／P16）是不同機型
                pair = f"{a.spec.brand} 「{min(a.spec.model, b.spec.model)}」與「{max(a.spec.model, b.spec.model)}」"
                if pair not in seen and SequenceMatcher(None, na, nb).ratio() >= 0.88:
                    seen.add(pair)
                    out.append(pair + "名稱很接近，可能是同一款")
                    if len(out) >= limit:
                        return out
    return out


def audit_sample(records: list[PanelRecord], now: datetime, n: int = 5) -> list[dict]:
    """抽樣稽核：每週固定種子隨機抽幾款，列出數值＋原文片段＋來源，方便人工 1 分鐘內核對。"""
    pool = [r for r in records if r.spec.evidence and r.source_url]
    rnd = random.Random(now.strftime("%G-%V"))
    pick = rnd.sample(pool, min(n, len(pool)))
    out = []
    for r in pick:
        fields = [f for f in ("refresh_hz", "sdr_nits", "res_w", "dci_p3_pct", "panel_tech", "surface", "size_inch",
                              "hdr_peak_nits", "panel_maker", "touch") if f in r.spec.evidence][:3]
        lines = []
        for f in fields:
            v = getattr(r.spec, f)
            if f == "res_w":
                v = f"{r.spec.res_w}×{r.spec.res_h}"
            lines.append({"field": FIELD_NAMES.get(f, f),
                          "value": ("有" if v else "無") if isinstance(v, bool) else (f"{v:g}" if isinstance(v, float) else v),
                          "quote": str(r.spec.evidence[f])[:140]})
        out.append({"model": f"{r.spec.brand or ''} {r.spec.model or ''}".strip(), "confidence": r.confidence.label,
                    "source_name": r.source_name, "source_url": r.source_url, "lines": lines})
    return out


def data_quality(records: list[PanelRecord], since: datetime, now: datetime,
                 not_duplicates: list | None = None) -> tuple[list[dict], dict]:
    recent = [r for r in records if r.last_updated >= since and r.confidence.rank >= Confidence.C.rank]
    checks = []

    # 原文依據覆蓋（防幻覺機制本身有沒有正常運作）
    no_ev = []
    filled_total = 0
    for r in recent:
        f = _filled(r)
        filled_total += len(f)
        miss = [x for x in f if x not in r.spec.evidence and x not in r.panel_spec_fields
                and not (x == "res_h" and "res_w" in r.spec.evidence)
                and not (x == "price_currency" and "price" in r.spec.evidence)  # 幣別以價格原文為依據
                and r.source_of(x).kind != "manual"]
        if miss:
            no_ev.append(f"{r.spec.brand} {r.spec.model}：{', '.join(FIELD_NAMES.get(x, x) for x in miss[:5])}")
    no_ev = list(dict.fromkeys(no_ev))
    checks.append(_check("原文依據", "bad" if len(no_ev) > max(2, len(recent) * 0.1) else ("warn" if no_ev else "ok"),
                         f"本期 {len(recent)} 款、{filled_total} 個欄位" +
                         ("，全部都有逐字原文依據" if not no_ev else f"，{len(no_ev)} 款有欄位缺原文依據"), no_ev[:8]))

    # AI 自動攔截：被刪掉的欄位（AI 讀錯或亂填，已被程式擋下）
    dropped = Counter()
    for r in recent:
        for fl in r.flags:
            if fl.startswith("無原文依據已移除"):
                dropped["無原文依據"] += 1
            elif fl.startswith("數值不合理已移除"):
                dropped["數值不合理"] += 1
    n_drop = sum(dropped.values())
    share = n_drop / (filled_total + n_drop) if filled_total + n_drop else 0
    checks.append(_check("自動攔截", "warn" if share > 0.15 else "ok",
                         f"AI 填的欄位有 {n_drop} 個被程式刪除（{share:.0%}）："
                         + "、".join(f"{k} {v}" for k, v in dropped.items()) if n_drop else "本期沒有需要攔截的欄位"))

    # 多來源佐證
    key_fields = ["size_inch", "res_w", "panel_tech", "refresh_hz", "sdr_nits", "hdr_peak_nits", "dci_p3_pct",
                  "srgb_pct", "panel_maker", "panel_model", "surface", "touch", "response_ms"]
    support, total_kf = 0, 0
    for r in recent:
        for f in key_fields:
            if getattr(r.spec, f, None) not in (None, [], ""):
                total_kf += 1
                support += bool(r.corroborated.get(f))
    corr = support / total_kf if total_kf else 0
    checks.append(_check("多來源佐證", "ok", f"本期重點規格中 {corr:.0%}（{support}/{total_kf}）已有第二個獨立網站寫出相同數值"
                         + ("（2026-10-18 起開始累積，之後逐週上升）" if not support else "")))

    # 來源數值不一致
    conflicts = list(dict.fromkeys(c for r in recent for fl in r.flags if (c := _conflict(r, fl))))
    checks.append(_check("來源不一致", "warn" if conflicts else "ok",
                         f"{len(conflicts)} 處不同來源寫的數值不同（保留可信度較高或先到的值）" if conflicts else "沒有發現來源互相矛盾",
                         conflicts[:10]))

    # 跨欄位矛盾
    issues = [f"{r.spec.brand} {r.spec.model}：{i}" for r in recent for i in consistency_issues(r)]
    checks.append(_check("跨欄位矛盾", "warn" if issues else "ok",
                         f"{len(issues)} 處規格彼此對不起來，建議人工確認" if issues else "沒有發現矛盾", issues[:10]))

    # 可能重複的機型
    dups = possible_duplicates([r for r in records if r.confidence.rank >= Confidence.C.rank], ignore=not_duplicates)
    checks.append(_check("重複機型", "warn" if dups else "ok",
                         f"{len(dups)} 組名稱很接近、可能沒合併" if dups else "沒有發現疑似重複", dups))

    stats = {"recent": len(recent), "corroboration": corr, "filled": filled_total, "dropped": n_drop}
    return checks, stats


def corrections_check(records: list[PanelRecord], cfg: dict) -> dict | None:
    from . import corrections
    n = len(corrections.items(cfg))
    miss, errs = corrections.unmatched(records, cfg)
    applied = sum(1 for r in records for fs in r.field_sources.values() if fs.kind == "manual")
    notes = [f"找不到機型：{m}（檢查品牌、機型名稱是否和週報上一樣）" for m in miss] + errs
    if not n and not notes:
        return None
    excluded = sum(1 for r in records if r.confidence == Confidence.E and any("排除" in f for f in r.flags))
    return _check("人工更正", "warn" if notes else "ok",
                  f"{n} 項更正，{applied} 個欄位以人工值為準" + (f"、{excluded} 款已排除（不列入報告）" if excluded else "")
                  + (f"；{len(notes)} 項需要修正設定" if notes else ""), notes)


def build(db: DB, cfg: dict, now: datetime, records: list[PanelRecord] | None = None, days: int = 7) -> dict:
    records = records if records is not None else db.all_panels()
    since = now - timedelta(days=days)
    ops = operations(db, cfg, now, days)
    quality, stats = data_quality(records, since.astimezone(timezone.utc), now,
                                  (cfg.get("health") or {}).get("not_duplicates"))
    cc = corrections_check(records, cfg)
    if cc:
        quality.append(cc)
    worst = max([c["level"] for c in ops + quality] or ["ok"], key=LEVEL_RANK.get)
    n_warn = sum(c["level"] != "ok" for c in ops + quality)
    summary = {"ok": "全部正常", "warn": f"{n_warn} 項需要注意", "bad": f"{n_warn} 項異常，其中有需要處理的問題"}[worst]
    return {"level": worst, "icon": LEVEL_ICON[worst], "summary": summary, "ops": ops, "quality": quality,
            "audit": audit_sample([r for r in records if r.last_updated >= since.astimezone(timezone.utc)
                                   and r.confidence.rank >= Confidence.C.rank], now, (cfg.get("health") or {}).get("audit_samples", 5)),
            "stats": stats}
