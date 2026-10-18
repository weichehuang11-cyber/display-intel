"""面板型號解碼與面板廠名稱統一。

筆電面板型號的開頭通常代表面板廠，數字常代表尺寸，例如：
  BOE NE160QDM-NY1 → BOE、16.0 吋
  Samsung Display ATNA60CL01 → Samsung Display、16.0 吋
  AUO B140HAN06 → AUO、14.0 吋
推算結果一律標示「推算」，不會覆蓋文章中明確寫出的數值。
"""

from __future__ import annotations

import re
from typing import Optional

# (正規表示式, 面板廠, 尺寸解析方式)
#   "3d"：三位數字，例如 160 → 16.0
#   "atna"：Samsung 兩位數字，例如 60 → 16.0、56 → 15.6
#   None：不推算尺寸
RULES = [
    (r"^ATNA(\d)(\d)", "Samsung Display", "atna"),
    (r"^NE(\d{3})", "BOE", "3d"),
    (r"^NV(\d{3})", "BOE", "3d"),
    (r"^TV(\d{3})", "BOE", "3d"),
    (r"^B(\d{3})[A-Z]{3}", "AUO", "3d"),
    (r"^N(\d{3})[A-Z]{3}", "Innolux", "3d"),
    (r"^LP(\d{3})", "LG Display", "3d"),
    (r"^LQ(\d{3})", "Sharp", "3d"),
    (r"^MN[A-Z]\d{3}", "CSOT", None),
]

MAKER_ALIASES = {
    "Samsung Display": ["samsung display", "samsung", "sdc", "三星顯示", "三星显示", "三星"],
    "LG Display": ["lg display", "lgd", "lg"],
    "BOE": ["boe", "京東方", "京东方"],
    "AUO": ["auo", "友達", "友达", "au optronics"],
    "Innolux": ["innolux", "群創", "群创"],
    "CSOT": ["csot", "tcl csot", "華星", "华星", "tcl 華星", "tcl华星", "tcl"],
    "Sharp": ["sharp", "夏普"],
    "Tianma": ["tianma", "天馬", "天马"],
    "Visionox": ["visionox", "維信諾", "维信诺"],
    "Everdisplay": ["everdisplay", "edo", "和輝", "和辉"],
    "JDI": ["jdi", "japan display"],
    "InfoVision": ["infovision", "ivo", "龍騰", "龙腾"],
    "HKC": ["hkc", "惠科"],
    "CEC Panda": ["cec panda", "熊貓", "熊猫"],
}

KNOWN_MAKERS = set(MAKER_ALIASES)

# 面板型號中「尺寸數字後面兩個字母」的解析度代碼，例如 LP160WQ1 → WQ、NE160WUM → WU
# 預設只放確認過的代碼；config.yaml 的 panel_res_codes 可增加（也可寫「AUO:UA」只套用某家面板廠）
DEFAULT_RES_CODES = {"WU": (1920, 1200), "WQ": (2560, 1600)}
RES_CODES: dict[str, tuple[int, int]] = dict(DEFAULT_RES_CODES)


def configure_res_codes(cfg: dict):
    RES_CODES.clear()
    RES_CODES.update(DEFAULT_RES_CODES)
    for code, res in (cfg.get("panel_res_codes") or {}).items():
        m = re.fullmatch(r"\s*(\d{3,4})\s*[x×*]\s*(\d{3,4})\s*", str(res))
        if m:
            RES_CODES[str(code).strip().upper()] = (int(m.group(1)), int(m.group(2)))

# EDID 面板識別碼（3 個字母廠商代碼＋4 位十六進位），評測網站常以這種格式標示，例如 BOE0AAE、SDC4189
EDID_VENDORS = {"BOE": "BOE", "AUO": "AUO", "SDC": "Samsung Display", "LGD": "LG Display", "CMN": "Innolux",
                "SHP": "Sharp", "CSO": "CSOT", "IVO": "InfoVision", "TMA": "Tianma", "NCP": "Visionox"}


def canonical_maker(name: Optional[str]) -> Optional[str]:
    """把各種寫法統一成標準面板廠名稱，例如「京东方」→「BOE」。認不得就原樣回傳。"""
    if not name:
        return None
    n = name.strip().lower()
    for canon, aliases in MAKER_ALIASES.items():
        if n == canon.lower() or n in aliases:
            return canon
    for canon, aliases in MAKER_ALIASES.items():
        if any(len(a) >= 3 and a in n for a in aliases):
            return canon
    return name.strip()


def decode(panel_model: Optional[str]) -> Optional[dict]:
    """從面板型號推算面板廠與尺寸。認不得回傳 None。"""
    if not panel_model:
        return None
    m_str = re.sub(r"[\s\-]", "", panel_model.upper())
    edid = re.fullmatch(r"([A-Z]{3})([0-9A-F]{4})", m_str)
    if edid and edid.group(1) in EDID_VENDORS:
        return {"maker": EDID_VENDORS[edid.group(1)], "size_inch": None, "res": None}
    for pattern, maker, size_mode in RULES:
        m = re.match(pattern, m_str)
        if not m:
            continue
        size = None
        if size_mode == "3d":
            size = int(m.group(1)) / 10
        elif size_mode == "atna":
            size = 10 + int(m.group(1)) + int(m.group(2)) / 10
        if size is not None and not 10 <= size <= 35:
            size = None
        res = None
        if size_mode == "3d":
            code = m_str[m.end(1):m.end(1) + 2]
            res = RES_CODES.get(f"{maker.upper()}:{code}") or RES_CODES.get(code)
        return {"maker": maker, "size_inch": size, "res": res}
    return None


# ---------- 從網頁原文找面板型號（不經 AI）----------
_EDID_RE = re.compile(r"\b(" + "|".join(EDID_VENDORS) + r")([0-9A-F]{4})\b")
_PART_RE = re.compile(r"\b((?:ATNA|NE|NV|TV|LP|LQ|MN[A-Z]|B|N)\d{2,3}[A-Z0-9]{2,8}(?:[-.][A-Z0-9]{1,6}){0,2})\b")


def find_panel_ids(text: str) -> list[dict]:
    """在原文中找面板型號與 EDID 識別碼。回傳 [{id, maker, size_inch, quote}]，quote 是原文片段（逐字）。"""
    out, seen = [], set()
    for m in list(_EDID_RE.finditer(text)) + list(_PART_RE.finditer(text)):
        pid = m.group(0)
        if pid in seen:
            continue
        if m.re is _EDID_RE and not re.search(r"\d", m.group(2)):
            continue  # 四碼都是字母，多半不是識別碼
        if m.re is _PART_RE and len(pid) < 8:
            continue  # 太短容易誤判（例如處理器、晶片組型號）
        dec = decode(pid)
        if not dec:
            continue
        seen.add(pid)
        start, end = max(0, m.start() - 40), min(len(text), m.end() + 40)
        quote = re.sub(r"\s+", " ", text[start:end]).strip()
        out.append({"id": pid, "maker": dec["maker"], "size_inch": dec["size_inch"], "res": dec.get("res"), "quote": quote})
    return out
