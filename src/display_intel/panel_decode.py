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
}


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
        return {"maker": maker, "size_inch": size}
    return None
