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
    ("平台顯示節能", "LRR、UBRR、DPST、OPST、PSR、Panel Replay 等平台節能技術"),
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
    ("色域", lambda r: bool(r.spec.srgb_pct or r.spec.dci_p3_pct or r.spec.adobe_rgb_pct or r.spec.ntsc_pct)),
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
    if spec.display_power_tech:
        out.add("平台顯示節能")
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
    "LRR": "Intel Low Refresh Rate，畫面靜止或變化少時降低刷新率以省電；不同版本的切換範圍與平順度不同。",
    "DPST": "Intel Display Power Saving Technology，依畫面內容調降背光並補償對比，以降低 LCD 耗電。",
    "OPST": "Intel OLED Power Saving Technology，針對 OLED 面板的節能技術。",
    "PSR／Panel Replay": "面板自刷新：畫面不變時由面板自行維持影像，讓顯示輸出暫停以省電。",
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
            "LRR": "Intel Low Refresh Rate，畫面靜止或變化少時降低刷新率以省電；不同版本的切換範圍與平順度不同。",
    "DPST": "Intel Display Power Saving Technology，依畫面內容調降背光並補償對比，以降低 LCD 耗電。",
    "OPST": "Intel OLED Power Saving Technology，針對 OLED 面板的節能技術。",
    "PSR／Panel Replay": "面板自刷新：畫面不變時由面板自行維持影像，讓顯示輸出暫停以省電。",
    "MUX／Advanced Optimus": "獨顯直連", "Intel Evo": "evo", "Copilot+ PC": "copilot+"}
    return [(k, GLOSSARY[k]) for k, needle in keys.items() if needle in t]


# ---------- 季度趨勢 ----------
import re as _re


def quarter_of(rec: PanelRecord) -> Optional[str]:
    """機型屬於哪一季：優先用文章寫的發表／上市時間，其次用新聞發布日；關注清單的既有機型若無時間則不計。"""
    lp = rec.spec.launch_period or ""
    m = _re.search(r"(20\d\d)\D{0,3}[Qq]([1-4])", lp) or _re.search(r"[Qq]([1-4])\D{0,3}(20\d\d)", lp)
    if m:
        y, q = (m.group(1), m.group(2)) if m.group(1).startswith("20") else (m.group(2), m.group(1))
        return f"{y} Q{q}"
    m = _re.search(r"(20\d\d)[-/年.\s]*(\d{1,2})", lp)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)} Q{(int(m.group(2)) - 1) // 3 + 1}"
    if rec.announced:
        return f"{rec.announced.year} Q{(rec.announced.month - 1) // 3 + 1}"
    return None


def _size_bucket(r):
    if r.spec.product_type == "aio":
        return "AIO"
    s = r.size_corrected or r.spec.size_inch or r.size_inferred
    if not s:
        return None
    if s < 13.9:
        return "13 吋以下"
    if s < 15.0:
        return "14 吋級"
    if s < 16.0:
        return "15.6 吋級"
    if s < 17.0:
        return "16 吋級"
    return "17–18 吋"


def _tech_bucket(r):
    f = features(r.spec)
    if "OLED" in f:
        return "OLED"
    if "Mini LED" in f:
        return "Mini LED"
    return "LCD" if r.spec.panel_tech else None


def _refresh_bucket(r):
    hz = r.spec.refresh_hz
    if not hz:
        return None
    return "60Hz" if hz <= 75 else "90–120Hz" if hz <= 120 else "144–180Hz" if hz < 240 else "240Hz 以上"


def _res_bucket(r):
    w = r.spec.res_w
    if not w:
        return None
    return "FHD 級" if w < 2200 else "2K–2.5K" if w < 2800 else "2.8K–3.2K" if w < 3400 else "4K 級"


def _aspect_bucket(r):
    a = r.aspect_ratio
    return a if a in ("16:10", "16:9", "3:2") else ("其他" if a else None)


def _surface_bucket(r):
    from .derive import surface_code
    c = surface_code(r.spec.surface)
    if not c:
        return None
    return "AGLR" if "AGLR" in c else "AG" if "AG" in c else "BV／AR" if ("BV" in c or "AR" in c) else c


# ---------- 價格帶 ----------
from .derive import DEFAULT_FX_TO_USD, price_usd  # noqa: E402

PRICE_CFG = {"fx": dict(DEFAULT_FX_TO_USD), "bands": [800, 1200, 1800, 2500]}


def configure_price(cfg: dict):
    """套用 config.yaml 的 price 設定（匯率與價格帶分界，美元）。"""
    pc = cfg.get("price") or {}
    PRICE_CFG["fx"] = {**DEFAULT_FX_TO_USD, **{k.upper(): float(v) for k, v in (pc.get("fx_to_usd") or {}).items()}}
    if pc.get("bands_usd"):
        PRICE_CFG["bands"] = sorted(int(b) for b in pc["bands_usd"])


def band_labels() -> list[str]:
    b = PRICE_CFG["bands"]
    out = [f"US${b[0]:,} 以下"]
    out += [f"US${lo:,}–{hi - 1:,}" for lo, hi in zip(b, b[1:])]
    return out + [f"US${b[-1]:,} 以上"]


def rec_price_usd(r) -> float | None:
    return price_usd(r.spec.price, r.spec.price_currency, PRICE_CFG["fx"])


def _price_bucket(r):
    usd = rec_price_usd(r)
    if usd is None:
        return None
    labels, bands = band_labels(), PRICE_CFG["bands"]
    for i, edge in enumerate(bands):
        if usd < edge:
            return labels[i]
    return labels[-1]


def price_spec_matrix(records: list, min_priced: int = 3) -> dict | None:
    """價位帶 × 規格：各價位帶的機型數與 OLED、≥120Hz、≥2.5K、觸控比例，以及亮度中位數。

    以機型為單位：價格取各選配最低起價，規格取資料最完整的選配。有標價的機型不到 min_priced 款時不顯示。
    """
    from statistics import median
    from .verify import _norm, model_norm
    models: dict = {}
    for r in records:
        if r.confidence.rank < Confidence.C.rank:
            continue
        k = (_norm(r.spec.brand), model_norm(r.spec))
        m = models.setdefault(k, {"best": r, "price": None})
        if completeness(r)[0] > completeness(m["best"])[0]:
            m["best"] = r
        usd = rec_price_usd(r)
        if usd and (m["price"] is None or usd < m["price"]):
            m["price"] = usd
    priced = [m for m in models.values() if m["price"]]
    if len(priced) < min_priced:
        return None
    labels, bands = band_labels(), PRICE_CFG["bands"]
    rows = []
    for i, label in enumerate(labels):
        lo = bands[i - 1] if i else 0
        hi = bands[i] if i < len(bands) else 10 ** 9
        ms = [m for m in priced if lo <= m["price"] < hi]
        if not ms:
            continue

        def share(pred, known):
            k = [m for m in ms if known(m["best"].spec)]
            return (round(100 * sum(1 for m in k if pred(m["best"])) / len(k)), len(k)) if k else (None, 0)
        sdr = [m["best"].spec.sdr_nits for m in ms if m["best"].spec.sdr_nits]
        rows.append({
            "band": label, "n": len(ms),
            "oled": share(lambda r: _tech_bucket(r) == "OLED", lambda s: bool(s.panel_tech)),
            "hz120": share(lambda r: (r.spec.refresh_hz or 0) >= 120, lambda s: bool(s.refresh_hz)),
            "res25": share(lambda r: (r.spec.res_w or 0) >= 2560, lambda s: bool(s.res_w)),
            "touch": share(lambda r: r.spec.touch is True, lambda s: s.touch is not None),
            "sdr": round(median(sdr)) if sdr else None,
            "examples": "、".join(f"{m['best'].spec.brand} {m['best'].spec.model}" for m in sorted(ms, key=lambda m: m["price"])[:3]),
        })
    return {"rows": rows, "priced": len(priced), "models": len(models)}


TREND_DIMENSIONS = [
    ("尺寸分布", _size_bucket, ["13 吋以下", "14 吋級", "15.6 吋級", "16 吋級", "17–18 吋", "AIO"]),
    ("面板技術", _tech_bucket, ["OLED", "Mini LED", "LCD"]),
    ("刷新率", _refresh_bucket, ["60Hz", "90–120Hz", "144–180Hz", "240Hz 以上"]),
    ("解析度", _res_bucket, ["FHD 級", "2K–2.5K", "2.8K–3.2K", "4K 級"]),
    ("長寬比", _aspect_bucket, ["16:10", "16:9", "3:2", "其他"]),
    ("表面處理", _surface_bucket, ["AG", "AGLR", "BV／AR"]),
    ("面板廠", lambda r: r.maker, None),
    ("價格帶", _price_bucket, None),  # 分類名稱依 config 的價格帶分界，於 quarterly_trends 內排序
]


def quarterly_trends(records: list[PanelRecord], max_quarters: int = 4, min_models: int = 5) -> dict:
    """各季新機的規格分布與季對季變化（百分點）。以機型為單位，每款取資料最完整的選配。"""
    from .verify import _norm, model_norm
    best: dict = {}
    for r in records:
        q = quarter_of(r)
        if not q or r.confidence.rank < Confidence.C.rank:
            continue
        k = (_norm(r.spec.brand), model_norm(r.spec))
        if k not in best or completeness(r)[0] > completeness(best[k])[0]:
            best[k] = r
    by_q: dict[str, list] = {}
    for r in best.values():
        by_q.setdefault(quarter_of(r), []).append(r)
    quarters = sorted(by_q)[-max_quarters:]
    usable = [q for q in quarters if len(by_q[q]) >= min_models]
    dims = []
    for name, fn, order in TREND_DIMENSIONS:
        rows = []
        all_vals = Counter()
        for q in quarters:
            vals = Counter(v for v in (fn(r) for r in by_q[q]) if v)
            all_vals.update(vals)
            rows.append({"quarter": q, "n": sum(vals.values()), "counts": vals})
        if name == "價格帶":
            order = band_labels()
        buckets = order or [b for b, _ in all_vals.most_common(6)]
        buckets = [b for b in buckets if all_vals.get(b)]
        if not buckets:
            continue
        for row in rows:
            row["share"] = {b: (row["counts"].get(b, 0) / row["n"]) if row["n"] else None for b in buckets}
        shift = {}
        if len(rows) >= 2 and rows[-1]["n"] >= min_models and rows[-2]["n"] >= min_models:
            for b in buckets:
                shift[b] = round((rows[-1]["share"][b] - rows[-2]["share"][b]) * 100)
        dims.append({"name": name, "buckets": buckets, "rows": rows, "shift": shift})
    return {"quarters": quarters, "models": {q: len(by_q[q]) for q in quarters}, "usable": usable, "dims": dims,
            "comparable": len(usable) >= 2}
