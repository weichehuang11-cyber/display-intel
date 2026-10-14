"""報告分析：關鍵數字、亮點、週對週比較、技術提及率、品牌 × 面板廠、傳聞追蹤。

所有數字都由資料庫計算，不經過 AI，避免編造。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable, Optional

from .schema import Confidence, PanelRecord, PanelSpec

# 追蹤的技術（名稱 → 判斷方式說明，給報告註腳用）
FEATURES = [
    ("OLED", "面板技術為 OLED"),
    ("Tandem OLED", "雙層疊層 OLED，亮度與壽命較高"),
    ("Mini LED", "Mini LED 背光 LCD"),
    ("高刷新 240Hz+", "刷新率 240Hz 以上"),
    ("高解析 2.8K+", "水平解析度 2880 以上"),
    ("低刷新節能", "LTPO、1Hz 或動態刷新等節能技術"),
    ("DC 調光／低閃爍", "DC 調光、高頻 PWM 或無閃爍認證"),
    ("HDR 認證", "VESA DisplayHDR 等認證"),
    ("觸控", "支援觸控"),
]


# 規格完整度的 13 項重點欄位（標籤 → 判斷是否已知）
KEY_FIELDS = [
    ("尺寸", lambda r: bool(r.spec.size_inch or r.size_inferred)),
    ("解析度", lambda r: bool(r.spec.res_w and r.spec.res_h)),
    ("面板技術", lambda r: bool(r.spec.panel_tech)),
    ("刷新率", lambda r: bool(r.spec.refresh_hz)),
    ("亮度", lambda r: bool(r.spec.sdr_nits or r.spec.hdr_peak_nits)),
    ("色域", lambda r: bool(r.spec.srgb_pct or r.spec.dci_p3_pct or r.spec.adobe_rgb_pct)),
    ("面板廠", lambda r: bool(r.maker)),
    ("面板型號", lambda r: bool(r.spec.panel_model)),
    ("反應時間", lambda r: bool(r.spec.response_ms)),
    ("調光方式", lambda r: bool(r.spec.dimming or r.spec.pwm_hz or r.spec.eye_care)),
    ("表面處理", lambda r: bool(r.spec.surface)),
    ("色準或 HDR", lambda r: bool(r.spec.delta_e or r.spec.certifications or r.spec.hdr_formats)),
    ("觸控", lambda r: r.spec.touch is not None),
]


def missing_fields(rec: PanelRecord) -> list[str]:
    return [label for label, known in KEY_FIELDS if not known(rec)]


def completeness(rec: PanelRecord) -> tuple[int, int]:
    """回傳（已知項數, 總項數）。"""
    total = len(KEY_FIELDS)
    return total - len(missing_fields(rec)), total


def mature_completeness(records: list[PanelRecord], now: datetime, min_days: int = 30, max_days: int = 120) -> tuple[Optional[float], int]:
    """上市滿 30 天（且 120 天內）機型的平均完整度，以每款資料最完整的選配計算。回傳（比例, 款數）。"""
    from .verify import _norm, model_norm
    best: dict = {}
    for r in records:
        age = now - r.first_seen
        if r.confidence.rank >= Confidence.C.rank and timedelta(days=min_days) <= age <= timedelta(days=max_days):
            k = (_norm(r.spec.brand), model_norm(r.spec))
            best[k] = max(completeness(r)[0], best.get(k, 0))
    if not best:
        return None, 0
    return sum(best.values()) / (len(best) * len(KEY_FIELDS)), len(best)


def to_fill(records: list[PanelRecord], now: datetime, days: int = 45, top: int = 5) -> list[dict]:
    """待補清單：近期機型中，重點欄位缺最多的幾款。"""
    from .verify import _norm, model_norm
    best: dict = {}
    for r in records:
        if r.first_seen >= now - timedelta(days=days) and r.confidence.rank >= Confidence.C.rank:
            k = (_norm(r.spec.brand), model_norm(r.spec))
            if k not in best or completeness(r)[0] > completeness(best[k])[0]:
                best[k] = r  # 同機型多種選配時，以資料最完整的那筆代表
    recent = sorted(best.values(), key=lambda r: (completeness(r)[0], -r.first_seen.timestamp()))
    return [{"name": f"{r.spec.brand} {r.spec.model}", "score": "%d/%d" % completeness(r),
             "missing": "、".join(missing_fields(r)[:6])} for r in recent[:top]]


def _low(v) -> str:
    if isinstance(v, list):
        return " ".join(map(str, v)).lower()
    return str(v or "").lower()


def features(spec: PanelSpec) -> set[str]:
    """判斷一筆規格提到了哪些追蹤中的技術。"""
    tech = _low(spec.panel_tech)
    out = set()
    if "oled" in tech:
        out.add("OLED")
    if "tandem" in tech or "tandem" in _low(spec.low_power_tech) or "雙層" in tech or "双层" in tech:
        out.add("Tandem OLED")
    if "mini" in tech or "mini" in _low(spec.backlight) or spec.mini_led_zones:
        out.add("Mini LED")
    if spec.refresh_hz and spec.refresh_hz >= 240:
        out.add("高刷新 240Hz+")
    if spec.res_w and spec.res_w >= 2880:
        out.add("高解析 2.8K+")
    from .ihv import low_refresh
    if low_refresh(spec):
        out.add("低刷新節能")
    care = _low(spec.dimming) + " " + _low(spec.eye_care)
    if "dc" in care or "flicker" in care or "無閃" in care or "无闪" in care or (spec.pwm_hz and spec.pwm_hz >= 1000):
        out.add("DC 調光／低閃爍")
    if "displayhdr" in _low(spec.certifications) or "hdr" in _low(spec.certifications):
        out.add("HDR 認證")
    if spec.touch:
        out.add("觸控")
    return out


def _brightness(spec: PanelSpec) -> Optional[float]:
    return spec.hdr_peak_nits or spec.sdr_nits


def kpis(rows: list[dict]) -> list[dict]:
    """報告開頭的關鍵數字。rows 為本期新機型卡片資料。"""
    recs = [r["rec"] for r in rows]
    known = [r for r in recs if r.spec.panel_tech]
    oled = sum(1 for r in known if "OLED" in features(r.spec))
    makers = Counter(r.maker for r in recs if r.maker)
    refresh = [r.spec.refresh_hz for r in recs if r.spec.refresh_hz]
    out = [{"label": "新機型", "value": str(len(recs)), "note": "款"}]
    out.append({"label": "OLED 占比", "value": f"{oled / len(known):.0%}" if known else "—",
                "note": f"{len(known)} 款有標示面板技術" if known else "資料不足"})
    if makers:
        name, n = makers.most_common(1)[0]
        out.append({"label": "供貨最多面板廠", "value": name, "note": f"{n} 款"})
    else:
        out.append({"label": "供貨最多面板廠", "value": "—", "note": "本期報導未提及"})
    out.append({"label": "刷新率中位數", "value": f"{median(refresh):g}Hz" if refresh else "—",
                "note": f"{len(refresh)} 款有標示" if refresh else "資料不足"})
    if recs:
        scores = [completeness(r) for r in recs]
        avg = sum(k for k, _ in scores) / sum(t for _, t in scores)
        out.append({"label": "平均規格完整度", "value": f"{avg:.0%}", "note": f"每款 {scores[0][1]} 項重點規格"})
    return out


def standouts(rows: list[dict], db_records: Iterable[PanelRecord], period_start: datetime) -> tuple[list[str], dict[str, list[str]]]:
    """本期亮點：最高刷新率、最亮、最高 PPI、首見技術。

    回傳（亮點句子清單, {紀錄 key: [卡片標籤]}）。
    """
    tags: dict[str, list[str]] = {}
    lines = []

    def tag(rec, text):
        tags.setdefault(rec.key, []).append(text)

    def name(rec):
        return f"{rec.spec.brand} {rec.spec.model}"

    recs = [r["rec"] for r in rows]
    with_refresh = [r for r in recs if r.spec.refresh_hz]
    if with_refresh:
        best = max(r.spec.refresh_hz for r in with_refresh)
        tops = [r for r in with_refresh if r.spec.refresh_hz == best]
        more = f" 等 {len(tops)} 款" if len(tops) > 1 else ""
        lines.append(f"最高刷新率：{name(tops[0])}{more}，{best:g}Hz")
        for t in tops:
            tag(t, "最高刷新率")
    with_b = [r for r in recs if _brightness(r.spec)]
    if with_b:
        top = max(with_b, key=lambda r: _brightness(r.spec))
        kind = "HDR 峰值" if top.spec.hdr_peak_nits else "亮度"
        lines.append(f"最亮：{name(top)}，{kind} {_brightness(top.spec):g} nits")
        tag(top, "最亮")
    with_ppi = [r for r in recs if r.ppi]
    if with_ppi:
        top = max(with_ppi, key=lambda r: r.ppi)
        lines.append(f"像素密度最高：{name(top)}，{top.ppi:g} PPI")
        tag(top, "最高 PPI")
    # 首見：這項技術在本期之前的紀錄中從未出現
    before = set()
    for r in db_records:
        if r.first_seen < period_start:
            before |= features(r.spec)
    if before:  # 資料庫有歷史資料時才判斷，避免第一週全部都是「首見」
        seen_now = {}
        for r in recs:
            for f in features(r.spec) - before:
                seen_now.setdefault(f, r)
        for f, r in seen_now.items():
            lines.append(f"首見技術：{f}（{name(r)}）")
            tag(r, f"首見 {f}")
    return lines, tags


def _period_stats(recs: list[PanelRecord]) -> dict:
    known = [r for r in recs if r.spec.panel_tech]
    refresh = [r.spec.refresh_hz for r in recs if r.spec.refresh_hz]
    sdr = [r.spec.sdr_nits for r in recs if r.spec.sdr_nits]
    return {
        "新機型數": len(recs),
        "OLED 占比": (sum(1 for r in known if "OLED" in features(r.spec)) / len(known)) if known else None,
        "刷新率中位數": median(refresh) if refresh else None,
        "SDR 亮度中位數": median(sdr) if sdr else None,
    }


def week_over_week(records: list[PanelRecord], period_start: datetime, days: int = 7) -> list[dict] | None:
    """本期與前一期比較。前一期沒有資料時回傳 None（資料累積中）。"""
    prev_start = period_start - timedelta(days=days)
    cur = [r for r in records if r.first_seen >= period_start and r.confidence.rank >= Confidence.C.rank]
    prev = [r for r in records if prev_start <= r.first_seen < period_start and r.confidence.rank >= Confidence.C.rank]
    if not prev:
        return None
    a, b = _period_stats(cur), _period_stats(prev)
    out = []
    for k in a:
        now_v, old_v = a[k], b[k]
        fmt = (lambda v: f"{v:.0%}") if "占比" in k else (lambda v: f"{v:g}" + ("Hz" if "刷新" in k else " nits" if "亮度" in k else ""))
        if now_v is None or old_v is None:
            arrow, delta = "", ""
        else:
            diff = now_v - old_v
            arrow = "▲" if diff > 0 else "▼" if diff < 0 else "＝"
            delta = (f"{diff * 100:+.0f} 個百分點" if "占比" in k else f"{diff:+g}")
        out.append({"metric": k, "now": fmt(now_v) if now_v is not None else "—",
                    "prev": fmt(old_v) if old_v is not None else "—", "arrow": arrow, "delta": delta})
    return out


def tech_mentions(records: list[PanelRecord], period_start: datetime, compare_days: int = 30) -> list[dict]:
    """技術提及率：本期新機型中有多少比例提到該技術，對照前 30 天。

    「提及率」而非「採用率」：報導沒提到不代表沒有。
    """
    cur = [r for r in records if r.first_seen >= period_start and r.confidence.rank >= Confidence.C.rank]
    base = [r for r in records if period_start - timedelta(days=compare_days) <= r.first_seen < period_start
            and r.confidence.rank >= Confidence.C.rank]
    out = []
    for f, desc in FEATURES:
        c = sum(1 for r in cur if f in features(r.spec))
        b = sum(1 for r in base if f in features(r.spec))
        if not c and not b:
            continue
        out.append({"feature": f, "desc": desc, "count": c,
                    "share": f"{c / len(cur):.0%}" if cur else "—",
                    "base_share": f"{b / len(base):.0%}" if base else "—"})
    return out


def brand_maker_matrix(records: list[PanelRecord], top_brands: int = 8) -> dict | None:
    """品牌 × 面板廠（累積全部紀錄，含型號推算）。"""
    pairs = Counter((r.spec.brand, r.maker) for r in records if r.maker and r.spec.brand)
    if not pairs:
        return None
    brands = [b for b, _ in Counter(b for (b, _m) in pairs.elements()).most_common(top_brands)]
    makers = [m for m, _ in Counter(m for (_b, m) in pairs.elements()).most_common()]
    rows = [{"brand": b, "cells": [pairs.get((b, m), 0) for m in makers]} for b in brands]
    inferred = sum(1 for r in records if r.panel_maker_inferred)
    return {"makers": makers, "rows": rows, "total": sum(pairs.values()), "inferred": inferred}


def rumor_tracker(records: list[PanelRecord], now: datetime, days: int = 90) -> list[dict]:
    """尚未證實的傳聞（近 90 天）。"""
    out = []
    for r in records:
        if r.confidence == Confidence.D and r.first_seen >= now - timedelta(days=days):
            out.append({"name": f"{r.spec.brand} {r.spec.model}",
                        "detail": " ".join(x for x in [r.spec.panel_tech or "", f"{r.spec.size_inch:g}\"" if r.spec.size_inch else "",
                                                       f"{r.spec.refresh_hz:g}Hz" if r.spec.refresh_hz else ""] if x),
                        "first_seen": r.first_seen.strftime("%Y-%m-%d"),
                        "days": (now - r.first_seen).days, "source_name": r.source_name, "source_url": r.source_url})
    return sorted(out, key=lambda x: x["first_seen"], reverse=True)


GLOSSARY = {
    "OLED": "有機發光二極體，每個像素自己發光，黑色更純、對比更高。",
    "Tandem OLED": "兩層發光層疊在一起，亮度更高、壽命更長、較省電。",
    "Mini LED": "用上千顆小型 LED 做背光的 LCD，可分區控光，亮度高。",
    "IPS": "LCD 的一種，可視角度廣、色彩穩定，筆電最常見。",
    "nits": "亮度單位。一般室內約 300–400 nits 足夠，戶外或 HDR 需要更高。",
    "DCI-P3": "電影業使用的色域標準，比 sRGB 更廣，覆蓋率越高顏色越鮮豔。",
    "sRGB": "網頁與一般內容的標準色域。",
    "ΔE（Delta E）": "色彩誤差，數字越小越準，小於 2 一般人難以分辨。",
    "PWM 調光": "用快速開關調整亮度，頻率太低可能讓敏感的人眼睛疲勞。",
    "DC 調光": "直接調整電流來控制亮度，不會閃爍。",
    "VRR": "可變刷新率，畫面更新與顯示卡同步，減少撕裂；G-SYNC、FreeSync 都屬於此類。",
    "LTPO": "可在 1Hz 到 120Hz 間切換的背板技術，靜態畫面時省電。",
    "PPI": "每英吋像素數，越高越細緻。由解析度和尺寸計算。",
    "MUX／Advanced Optimus": "讓獨立顯示卡直接輸出到螢幕，遊戲延遲較低、效能較好；一般 Optimus 則經過內顯轉送。",
    "Intel Evo": "Intel 筆電認證，對螢幕、續航、喚醒速度等有最低要求。",
    "Copilot+ PC": "Microsoft 的 AI PC 認證，需要具備一定效能的 NPU。",
    "G8.6 世代線": "第 8.6 代面板產線，玻璃基板較大，可更有效率地切割筆電尺寸 OLED。",
}


def glossary_for(text: str) -> list[tuple[str, str]]:
    """只列出報告中實際出現的名詞。"""
    t = text.lower()
    keys = {"OLED": "oled", "Tandem OLED": "tandem", "Mini LED": "mini led", "IPS": "ips", "nits": "nits",
            "DCI-P3": "dci-p3", "sRGB": "srgb", "ΔE（Delta E）": "δe", "PWM 調光": "pwm", "DC 調光": "dc 調光",
            "VRR": "sync", "LTPO": "ltpo", "PPI": "ppi", "G8.6 世代線": "8.6",
            "MUX／Advanced Optimus": "獨顯直連", "Intel Evo": "evo", "Copilot+ PC": "copilot+"}
    return [(k, GLOSSARY[k]) for k, needle in keys.items() if needle in t]
