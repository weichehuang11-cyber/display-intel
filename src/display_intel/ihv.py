"""平台適配性評比：螢幕規格與 Intel / NVIDIA / AMD / Qualcomm 平台功能的搭配。

原則：
- 只根據公開資料判斷。沒寫＝「?」未知，不會當成不支援
- 不打主觀分數，只標示每一項是否已確認
- 標示符號：✓ 已確認支援　✗ 明確不支援或資訊互相矛盾　? 未知　— 不適用
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

from .schema import Confidence, PanelRecord, PanelSpec

CPU_VENDORS = [("Qualcomm", ("snapdragon", "qualcomm")), ("Apple", ("apple m", "m1", "m2", "m3", "m4", "m5")),
               ("AMD", ("ryzen", "amd")), ("Intel", ("intel", "core ultra", "core i", "lunar lake", "arrow lake", "panther lake")),
               ("MediaTek", ("mediatek", "kompanio"))]
GPU_VENDORS = [("NVIDIA", ("nvidia", "geforce", "rtx", "gtx")), ("AMD", ("radeon rx", "radeon 8", "radeon 7", "amd radeon")),
               ("Intel", ("intel arc", "arc a", "arc b"))]


def _low(v) -> str:
    if isinstance(v, list):
        return " ".join(map(str, v)).lower()
    return str(v or "").lower()


def cpu_vendor(spec: PanelSpec) -> Optional[str]:
    t = _low(spec.cpu_platform)
    if not t:
        return None
    for name, keys in CPU_VENDORS:
        if name == "Apple" and not t.startswith(("apple", "m1", "m2", "m3", "m4", "m5")):
            continue
        if any(k in t for k in keys):
            return name
    return None


def gpu_vendor(spec: PanelSpec) -> Optional[str]:
    """獨立顯示卡廠商；沒有獨顯資訊回傳 None。"""
    t = _low(spec.gpu)
    if not t or "integrated" in t or "內顯" in t or "内显" in t:
        return None
    for name, keys in GPU_VENDORS:
        if any(k in t for k in keys):
            return name
    return None


# 平台顯示節能技術：別名 → 標準名稱、所屬平台（None 表示各平台通用）
POWER_TECH = [
    (r"\blrr\b|low[\s-]?refresh[\s-]?rate", "LRR", "Intel"),
    (r"\bubrr\b", "UBRR", "Intel"),
    (r"\bdpst\b|display power sav", "DPST", "Intel"),
    (r"\bopst\b|oled power sav", "OPST", "Intel"),
    (r"\bs?drrs\b|refresh rate switching", "DRRS", "Intel"),
    (r"panel replay", "Panel Replay", None),
    (r"\bpsr\s*2\b|psr2|panel self[\s-]?refresh\s*2", "PSR2", None),
    (r"\bpsr\b|panel self[\s-]?refresh", "PSR", None),
    (r"vari[\s-]?bright", "Vari-Bright", "AMD"),
]


def normalize_power_tech(items: Optional[list[str]]) -> list[str]:
    """統一寫法並保留版本，例如「Intel Low Refresh Rate 2.0」→「LRR 2.0」。"""
    out = []
    for raw in items or []:
        t = str(raw).strip()
        low = t.lower()
        for pattern, name, _vendor in POWER_TECH:
            if re.search(pattern, low):
                ver = re.search(r"(\d+(?:\.\d+)?)", re.sub(r"psr\s*2|panel self[\s-]?refresh\s*2", "", low))
                label = f"{name} {ver.group(1)}" if ver and name not in ("PSR2",) else name
                break
        else:
            label = t
        if label not in out:
            out.append(label)
    return out


def power_tech_vendor(label: str) -> Optional[str]:
    for pattern, name, vendor in POWER_TECH:
        if label.split()[0] == name:
            return vendor
    return None


def low_refresh(spec: PanelSpec) -> bool:
    """面板是否支援低刷新／動態刷新等節能技術（VRR 範圍從 1Hz 起算也算）。"""
    lp = _low(spec.low_power_tech) + " " + _low(spec.display_power_tech)
    if any(k in lp for k in ("ltpo", "1hz", "動態刷新", "动态刷新", "dynamic refresh", "psr", "self refresh", "自刷新",
                             "lrr", "low refresh", "drrs", "panel replay")):
        return True
    return bool(re.match(r"\s*1\s*(hz)?\s*[-–~]", _low(spec.vrr_range)))


def platform_text(spec: PanelSpec) -> str:
    parts = [spec.cpu_platform or "", spec.gpu or ""]
    if spec.gpu_switching:
        parts.append(spec.gpu_switching)
    return "・".join(p for p in parts if p)


def evaluate(spec: PanelSpec) -> list[dict]:
    """五項適配性評比。"""
    items = []
    vrr = _low(spec.vrr_tech) + " " + _low(spec.vrr_range)
    gv, cv = gpu_vendor(spec), cpu_vendor(spec)

    # 1. 可變刷新率
    if "g-sync" in vrr and gv not in (None, "NVIDIA"):
        items.append(("可變刷新率", "✗", "標示 G-SYNC，但獨顯不是 NVIDIA，資訊可能有誤"))
    elif vrr.strip():
        items.append(("可變刷新率", "✓", "、".join(x for x in [_low(spec.vrr_tech) and "／".join(spec.vrr_tech or []), spec.vrr_range or ""] if x)))
    elif spec.refresh_hz and spec.refresh_hz >= 120:
        items.append(("可變刷新率", "?", f"{spec.refresh_hz:g}Hz 高刷新，但未標示 VRR"))
    else:
        items.append(("可變刷新率", "?", ""))

    # 2. 獨顯直連（降低遊戲延遲）
    sw = _low(spec.gpu_switching)
    if not spec.gpu:
        items.append(("獨顯直連", "—" if spec.cpu_platform else "?", "無獨顯資訊" if spec.cpu_platform else ""))
    elif gv is None:
        items.append(("獨顯直連", "—", "只有內顯"))
    elif "mux" in sw or "advanced optimus" in sw or "直連" in sw:
        items.append(("獨顯直連", "✓", spec.gpu_switching))
    elif sw and ("optimus" in sw or "hybrid" in sw):
        items.append(("獨顯直連", "✗", f"{spec.gpu_switching}（無直連）"))
    else:
        items.append(("獨顯直連", "?", f"{gv} 獨顯，未標示切換方式"))

    # 3. 節能搭配（面板低刷新／動態刷新）
    techs = normalize_power_tech(spec.display_power_tech)
    mismatch = [t for t in techs if power_tech_vendor(t) and cv and power_tech_vendor(t) != cv]
    if mismatch:
        items.append(("節能搭配", "✗", f"標示 {'、'.join(mismatch)}，但處理器平台是 {cv}，資訊可能有誤"))
    elif techs:
        items.append(("節能搭配", "✓", "、".join(techs)))
    elif low_refresh(spec):
        items.append(("節能搭配", "✓", "面板支援低刷新或動態刷新" + (f"（{cv} 平台）" if cv else "")))
    else:
        items.append(("節能搭配", "?", ""))

    # 4. HDR 與色彩
    cert = _low(spec.certifications) + " " + _low(spec.hdr_formats)
    if "displayhdr" in cert or "hdr" in cert or "dolby vision" in cert:
        items.append(("HDR", "✓", "、".join((spec.certifications or []) + (spec.hdr_formats or []))))
    else:
        items.append(("HDR", "?", "OLED 具 HDR 能力，但未標示認證" if "oled" in _low(spec.panel_tech) else ""))

    # 5. 平台認證
    if spec.platform_certifications:
        items.append(("平台認證", "✓", "、".join(spec.platform_certifications)))
    else:
        items.append(("平台認證", "?", ""))
    return [{"name": n, "status": s, "note": note} for n, s, note in items]


def badge_line(items: list[dict]) -> str:
    return "　".join(f"{i['name']} {i['status']}" for i in items)


def platform_matrix(records: list[PanelRecord], now: datetime, days: int = 60) -> Optional[dict]:
    """平台 × 面板對照：近 60 天機型依處理器平台分組（每款取資料最完整的選配）。"""
    from .analytics import completeness, features
    from .verify import _norm, model_norm
    best: dict = {}
    for r in records:
        if r.confidence.rank >= Confidence.C.rank and r.first_seen >= now - timedelta(days=days):
            k = (_norm(r.spec.brand), model_norm(r.spec))
            if k not in best or completeness(r)[0] > completeness(best[k])[0]:
                best[k] = r
    groups: dict[str, list[PanelRecord]] = {}
    for r in best.values():
        groups.setdefault(cpu_vendor(r.spec) or "未標示", []).append(r)
    if set(groups) == {"未標示"}:
        return None

    def pct(rs, fn):
        known = [r for r in rs if fn(r) is not None]
        return f"{sum(1 for r in known if fn(r)) / len(known):.0%}" if known else "—"

    order = ["Intel", "AMD", "Qualcomm", "Apple", "MediaTek", "未標示"]
    rows = []
    for v in sorted(groups, key=lambda x: order.index(x) if x in order else 99):
        rs = groups[v]
        gpu_counts = Counter(gpu_vendor(r.spec) or ("內顯" if r.spec.gpu or r.spec.cpu_platform else "未知") for r in rs)
        rows.append({
            "vendor": v, "count": len(rs),
            "oled": pct(rs, lambda r: ("OLED" in features(r.spec)) if r.spec.panel_tech else None),
            "high_refresh": pct(rs, lambda r: (r.spec.refresh_hz >= 144) if r.spec.refresh_hz else None),
            "vrr": f"{sum(1 for r in rs if evaluate(r.spec)[0]['status'] == '✓')}/{len(rs)}",
            "gpu": "、".join(f"{k} {n}" for k, n in gpu_counts.most_common(3)),
        })
    return {"rows": rows, "days": days}
