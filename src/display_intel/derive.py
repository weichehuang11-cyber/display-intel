"""衍生規格計算與合理性檢查。

衍生規格一律由程式計算，不從文章抄。合理性檢查用來攔下 AI 讀錯或文章筆誤的數值。
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Optional

from .schema import PanelSpec

COMMON_RATIOS = {"16:9": 16 / 9, "16:10": 16 / 10, "3:2": 3 / 2, "4:3": 4 / 3, "21:9": 64 / 27, "5:4": 5 / 4}

# 欄位 → (最小值, 最大值)。超出範圍的數值會被移除並標記。
PLAUSIBLE_RANGES = {
    "size_inch": (10, 35),
    "res_w": (1024, 7680),
    "res_h": (600, 4320),
    "sdr_nits": (150, 2000),
    "hdr_peak_nits": (300, 4000),
    "srgb_pct": (40, 200),
    "dci_p3_pct": (30, 150),
    "adobe_rgb_pct": (30, 150),
    "ntsc_pct": (30, 150),
    "color_depth_bit": (6, 12),
    "delta_e": (0, 10),
    "refresh_hz": (30, 540),
    "response_ms": (0.01, 50),
    "mini_led_zones": (16, 10000),
    "pwm_hz": (50, 100000),
    "screen_to_body_pct": (50, 100),
}

# 不需要原文依據的欄位（用於識別或由其他欄位推得）
NO_EVIDENCE_NEEDED = {"evidence", "product_type", "regions"}


def ppi(res_w: Optional[int], res_h: Optional[int], size_inch: Optional[float]) -> Optional[float]:
    if not (res_w and res_h and size_inch):
        return None
    return round(math.hypot(res_w, res_h) / size_inch, 1)


def aspect_ratio(res_w: Optional[int], res_h: Optional[int]) -> Optional[str]:
    if not (res_w and res_h):
        return None
    r = res_w / res_h
    name, val = min(COMMON_RATIOS.items(), key=lambda kv: abs(kv[1] - r))
    if abs(val - r) < 0.015:
        return name
    g = math.gcd(res_w, res_h)
    return f"{res_w // g}:{res_h // g}"


def density_class(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if value < 150:
        return "一般"
    if value <= 220:
        return "高"
    return "極高"


def plausibility_issues(spec: PanelSpec) -> list[str]:
    """回傳超出合理範圍的欄位清單（欄位名稱）。"""
    bad = []
    for field, (lo, hi) in PLAUSIBLE_RANGES.items():
        v = getattr(spec, field)
        if v is not None and not (lo <= v <= hi):
            bad.append(field)
    p = ppi(spec.res_w, spec.res_h, spec.size_inch)
    if p is not None and p > 400:
        bad.append("ppi")
    if spec.sdr_nits and spec.hdr_peak_nits and spec.hdr_peak_nits < spec.sdr_nits:
        bad.append("hdr_peak_nits")
    return bad


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def quote_in_text(quote: str, text: str) -> bool:
    """原文片段是否真的出現在文章中（忽略大小寫、全半形與空白差異）。"""
    q = _norm(quote)
    if len(q) < 2:
        return False
    return q in _norm(text)


def enforce_evidence(spec: PanelSpec, text: str) -> tuple[PanelSpec, list[str]]:
    """移除沒有原文依據的欄位，回傳（清理後的規格, 被移除的欄位）。

    這是防止 AI 幻覺的主要機制：每個填入的欄位都必須附上逐字出自文章的片段。
    """
    data = spec.model_dump()
    evidence = {k: v for k, v in spec.evidence.items() if isinstance(v, str) and quote_in_text(v, text)}
    dropped = []
    for field, value in data.items():
        if field in NO_EVIDENCE_NEEDED or value in (None, [], ""):
            continue
        if field in ("brand", "model"):
            # 品牌、機型名稱只要出現在文章中即可
            if quote_in_text(str(value), text) or field in evidence:
                continue
        elif field in evidence:
            continue
        data[field] = None
        dropped.append(field)
    data["evidence"] = evidence
    return PanelSpec(**data), dropped


def clean_spec(spec: PanelSpec, text: str) -> tuple[PanelSpec, list[str]]:
    """依序執行：原文依據檢查 → 合理性檢查。回傳清理後的規格與問題標記。"""
    spec, dropped = enforce_evidence(spec, text)
    flags = [f"無原文依據已移除：{f}" for f in dropped]
    bad = plausibility_issues(spec)
    if bad:
        data = spec.model_dump()
        for f in bad:
            if f in data:
                data[f] = None
        spec = PanelSpec(**data)
        flags += [f"數值不合理已移除：{f}" for f in bad]
    return spec, flags


# ---------- 表面處理代碼 ----------
# 業界常用代碼：AG 霧面／抗眩光、AGLR 抗眩光低反射、AR 抗反射鍍膜、BV 亮面（BrightView／Glossy）
SURFACE_LABELS = {"AG": "霧面抗眩光", "AGLR": "抗眩光低反射", "AR": "抗反射鍍膜", "BV": "亮面"}


def surface_code(surface: Optional[str]) -> Optional[str]:
    """把文章寫的表面處理統一成 AG / AGLR / AR / BV（可組合，例如 BV+AR）。認不得回傳 None。"""
    if not surface:
        return None
    t = _norm(surface)
    codes = []
    low_reflect = bool(re.search(r"\baglr\b|low[\s-]?reflect|低反射", t))
    anti_glare = bool(re.search(r"\bag\b|anti[\s-]?glare|matte|non[\s-]?glare|霧面|雾面|防眩|抗眩", t))
    anti_reflect = bool(re.search(r"\bar\b|anti[\s-]?reflect|抗反射|防反射|減反射|减反射", t))
    glossy = bool(re.search(r"\bbv\b|bright[\s-]?view|gloss|亮面|鏡面|镜面|光面", t))
    if low_reflect and (anti_glare or "aglr" in t):
        codes.append("AGLR")
    elif anti_glare:
        codes.append("AG")
    if glossy and not anti_glare:
        codes.insert(0, "BV")
    if anti_reflect and "AGLR" not in codes:
        codes.append("AR")
    if low_reflect and not codes:
        codes.append("AR")  # 只寫「低反射」視為抗反射處理
    return "+".join(codes) or None
