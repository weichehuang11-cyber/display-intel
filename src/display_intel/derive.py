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
    "reflectance_pct": (0.05, 20),
    "haze_pct": (0, 95),
    "uniformity_pct": (50, 100),
    "panel_thickness_mm": (0.1, 10),
    "vrr_min_hz": (1, 120),
    "panel_power_w": (0.3, 40),
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

# ---------- 價格 ----------
# 概略匯率（換算美元用來分價格帶；config.yaml 的 price.fx_to_usd 可覆蓋）
DEFAULT_FX_TO_USD = {"USD": 1.0, "TWD": 0.031, "CNY": 0.14, "EUR": 1.08, "JPY": 0.0067, "GBP": 1.27, "KRW": 0.00073,
                     "INR": 0.012, "AUD": 0.66, "CAD": 0.73, "HKD": 0.128, "SGD": 0.74}
_CURRENCY_HINTS = [  # (正規表示式, 幣別)；順序重要：NT$、US$、HK$ 要在 $ 之前
    (r"nt\$|ntd|twd|新台幣|新臺幣|台幣|臺幣", "TWD"), (r"hk\$|hkd|港幣|港元", "HKD"), (r"(?<![a-z])s\$|sgd", "SGD"),
    (r"(?<![a-z])a\$|aud", "AUD"), (r"(?<![a-z])c\$|cad", "CAD"), (r"us\$|usd|美元|美金", "USD"),
    (r"rmb|cny|人民币|人民幣|元", "CNY"), (r"jpy|円|税込", "JPY"), (r"eur|€|欧元|歐元", "EUR"),
    (r"gbp|£|英镑|英鎊", "GBP"), (r"krw|₩|원", "KRW"), (r"inr|₹|rs\.", "INR"), (r"\$", "USD"),
]


def normalize_currency(*texts: Optional[str]) -> Optional[str]:
    """從 AI 填的幣別或價格原文判斷幣別代碼。認不得回傳 None。"""
    for t in texts:
        if not t:
            continue
        code = str(t).strip().upper()
        if code in DEFAULT_FX_TO_USD:
            return code
        low = str(t).lower()
        yen = re.search(r"[¥￥]\s*([\d,]+)", low)
        if yen and not re.search(r"円|jpy|rmb|cny|元", low):
            amount = int(yen.group(1).replace(",", "") or 0)
            return "JPY" if amount >= 50000 else "CNY"  # 日圓筆電價格多為六位數，人民幣多為四到五位數
        for pat, cur in _CURRENCY_HINTS:
            if re.search(pat, low):
                return cur
    return None


def price_usd(price: Optional[float], currency: Optional[str], fx: Optional[dict] = None) -> Optional[float]:
    rate = (fx or DEFAULT_FX_TO_USD).get(currency or "")
    return round(price * rate) if price and rate else None


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


# 16:9 面板的標準對角線：文章常把 17.3 吋寫成 17 吋、15.6 寫成 15、16.1 寫成 16、13.3 寫成 13
STANDARD_16_9 = {13: 13.3, 15: 15.6, 16: 16.1, 17: 17.3}


def actual_size(size: Optional[float], res_w: Optional[int], res_h: Optional[int],
                decoded: Optional[float] = None) -> Optional[float]:
    """文章寫的是整數吋時，推算實際對角線。回傳 None 表示不需修正。

    1. 面板型號推算的尺寸和文章相差 0.6 吋以內 → 以型號為準（例如 17 吋＋NE173… → 17.3）
    2. 否則只在解析度是 16:9 時，套用 16:9 標準尺寸（16:10 的 17 吋是真的 17.0，例如 LG gram 17）
    """
    if not size:
        return None
    if decoded and decoded != size and abs(decoded - size) <= 0.6:
        return decoded
    if size == int(size) and aspect_ratio(res_w, res_h) == "16:9":
        return STANDARD_16_9.get(int(size))
    return None


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


# ---------- 數值必須出現在原文片段 ----------
NUMERIC_FIELDS = [f for f, fi in PanelSpec.model_fields.items()
                  if str(fi.annotation) in ("typing.Optional[float]", "typing.Optional[int]")]
# 不會混淆的標準解析度名稱（「4K」「2.8K」「QHD+」「FHD+」有歧義，不接受）
RES_NAMES = [(r"\bwuxga\b", (1920, 1200)), (r"\bwqxga\b", (2560, 1600)), (r"\bwqhd\b(?!\+)|\bqhd\b(?!\+)|\b1440p\b", (2560, 1440)),
             (r"\buhd\b(?!\+)|\b2160p\b", (3840, 2160)), (r"\bfull[\s-]?hd\b(?!\+)|\bfhd\b(?!\+)|\b1080p\b|全高清(?!\+)", (1920, 1080))]
# 色數與色深的標準換算
COLOR_COUNTS = [(r"16\.7\s*(?:million|m\b)|1670\s*万|1670\s*萬", 8), (r"1\.07\s*(?:billion|b\b)|10\.7\s*亿|10\.7\s*億", 10)]


def quote_numbers(quote: str) -> set[float]:
    """原文片段中的數字（處理千分位 1,920／1.499 與全形字）。"""
    q = unicodedata.normalize("NFKC", quote)
    q = re.sub(r"(?<=\d)[,.](?=\d{3}(?!\d))", "", q)
    # 緊接在英文字母後面的數字是型號的一部分（ProArt P16、ROG G14），不算
    #（「2560x1600」的 x 是乘號，例外）
    return {float(x) for x in re.findall(r"(?:(?<![A-Za-z\d.])|(?<=\d[xX]))\d+(?:\.\d+)?", q)}


def value_supported(field: str, value, quote: str, res: tuple | None = None) -> bool:
    """數值欄位：原文片段裡要真的有這個數字（或不會混淆的標準名稱／換算）。"""
    if value is None or field not in NUMERIC_FIELDS:
        return True
    if float(value) in quote_numbers(quote):
        return True
    low = unicodedata.normalize("NFKC", quote).lower()
    if field in ("res_w", "res_h") and res:
        return any(re.search(p, low) and r == res for p, r in RES_NAMES)
    if field == "color_depth_bit":
        return any(re.search(p, low) and b == int(value) for p, b in COLOR_COUNTS)
    return False


def unsupported_values(spec: PanelSpec, skip: set | frozenset = frozenset()) -> list[str]:
    """有原文片段、但片段裡沒有這個數字的欄位（解析度寬高一起判斷）。"""
    bad = []
    res = (spec.res_w, spec.res_h) if spec.res_w and spec.res_h else None
    # 解析度寬高合併判斷（來源常把「3,840×2,400」拆成兩段引用）
    res_quote = " ".join(str(spec.evidence[k]) for k in ("res_w", "res_h") if spec.evidence.get(k))
    for f in NUMERIC_FIELDS:
        v = getattr(spec, f)
        if v is None or f in skip:
            continue
        quote = res_quote if f in ("res_w", "res_h") else spec.evidence.get(f)
        if quote and not value_supported(f, v, str(quote), res):
            bad.append(f)
    if "res_w" in bad or "res_h" in bad:
        bad = [f for f in bad if f not in ("res_w", "res_h")] + [f for f in ("res_w", "res_h") if getattr(spec, f) is not None]
    return bad


def find_res_quote(text: str, w: int, h: int) -> Optional[str]:
    """在全文找明確寫出的解析度（例如「3,840 x 2,400」「2880×1800」），回傳原文片段。"""
    fmt = lambda n: f"{n:,}".replace(",", r"[,.]?")
    m = re.search(rf"{fmt(w)}\s*(?:[x×*]|by)\s*{fmt(h)}", unicodedata.normalize("NFKC", text), re.I)
    if not m:
        return None
    t = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", t[max(0, m.start() - 15):m.end() + 15]).strip()


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
        elif field == "price_currency" and "price" in evidence:
            continue  # 幣別以價格原文為依據
        elif field in evidence:
            continue
        data[field] = None
        dropped.append(field)
    data["evidence"] = evidence
    spec = PanelSpec(**data)
    # 原文片段裡要真的有這個數字（擋下「4K」被換算成 3840×2160、新聞標題被當成尺寸依據這類情況）
    bad = unsupported_values(spec)
    if bad and spec.res_w and spec.res_h and "res_w" in bad:
        # AI 引用的片段沒有數字，但全文其他地方明確寫了 → 改用那段原文當依據
        q = find_res_quote(text, spec.res_w, spec.res_h)
        if q:
            data = spec.model_dump()
            data["evidence"]["res_w"] = data["evidence"]["res_h"] = q
            spec = PanelSpec(**data)
            bad = unsupported_values(spec)
    if bad:
        data = spec.model_dump()
        for f in bad:
            data[f] = None
            data["evidence"].pop(f, None)
        spec = PanelSpec(**data)
        dropped += [f"{f}（原文片段沒有這個數字）" for f in bad]
    return spec, dropped


def clean_price(spec: PanelSpec) -> tuple[PanelSpec, list[str]]:
    """統一幣別；認不得幣別或換算後不合理（低於 US$150 或高於 US$10,000）就移除價格。"""
    if not spec.price:
        return spec, []
    cur = normalize_currency(spec.price_currency, spec.evidence.get("price"))
    usd = price_usd(spec.price, cur)
    data = spec.model_dump()
    if usd is None or not 150 <= usd <= 10000:
        data["price"] = data["price_currency"] = None
        data["evidence"].pop("price", None)
        return PanelSpec(**data), [f"價格無法判斷幣別或不合理已移除：{spec.price} {spec.price_currency or ''}".strip()]
    data["price_currency"] = cur
    return PanelSpec(**data), []


def clean_spec(spec: PanelSpec, text: str) -> tuple[PanelSpec, list[str]]:
    """依序執行：原文依據檢查 → 價格幣別 → 合理性檢查。回傳清理後的規格與問題標記。"""
    spec, dropped = enforce_evidence(spec, text)
    flags = [f"無原文依據已移除：{f}" for f in dropped]
    spec, pf = clean_price(spec)
    flags += pf
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


