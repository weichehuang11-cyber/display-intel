"""範例模式：不連網、不需金鑰，用虛構資料走一遍「查核 → 報告」流程，讓你先看報告長相。

範例中的品牌與機型都是虛構的，不代表真實產品。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

from . import verify
from .db import DB
from .report import build_report
from .schema import Extraction, PanelFab, PanelSpec, PlatformTech, RawArticle


def _article(title: str, source: str, source_type: str, body: str) -> RawArticle:
    return RawArticle(url=f"https://example.com/{abs(hash(title))}", title=title, source=source,
                      source_type=source_type, published=datetime.now(timezone.utc), content=body, content_level="full")


def sample_results() -> list[tuple[RawArticle, Extraction, list[str]]]:
    a1 = _article("範例品牌 A 發表 Aero 16 筆電", "範例品牌 A 新聞室", "official",
                  "Aero 16 採用 16 吋 2880x1800 OLED 面板，120Hz 刷新率，SDR 亮度 500 nits，HDR 峰值 1100 nits，"
                  "100% DCI-P3，通過 DisplayHDR True Black 600，採用 DC 調光。面板由 Samsung Display 供應。")
    a2 = _article("Review: Example B Volt 14 display measured", "範例評測網", "review",
                  "The Volt 14 uses a 14-inch 2560x1600 IPS panel at 165Hz. We measured 412 nits and 98% sRGB. "
                  "The panel is a BOE NE140QDM-NY1. No PWM was detected.")
    a3 = _article("爆料：範例品牌 C 新款 AIO 將用 27 吋 Mini LED", "社群爆料帳號", "social",
                  "傳出範例品牌 C 新一代 AIO 將採用 27 吋 3840x2160 Mini LED 面板，1152 區背光，144Hz。")
    a4 = _article("Example vendor announces new laptop panel power-saving feature", "範例平台廠部落格", "official",
                  "The new display power feature lets compatible OLED laptops drop the refresh rate to 1Hz on static content, "
                  "extending battery life on next-generation platforms.")
    a5 = _article("範例面板廠 G8.6 IT OLED 產線進入試產", "範例產業媒體", "media",
                  "範例面板廠第 8.6 代 IT OLED 產線已開始試產，設計產能每月 32,000 片玻璃基板，預計明年量產，主要供應筆電與平板。")

    ex1 = Extraction(relevant=True, panels=[PanelSpec(
        brand="範例品牌 A", model="Aero 16", product_type="laptop", panel_maker="Samsung Display", panel_tech="OLED",
        size_inch=16, res_w=2880, res_h=1800, refresh_hz=120, sdr_nits=500, hdr_peak_nits=1100, dci_p3_pct=100,
        certifications=["DisplayHDR True Black 600"], dimming="DC",
        evidence={"panel_maker": "Samsung Display", "panel_tech": "OLED 面板", "size_inch": "16 吋", "res_w": "2880x1800",
                  "res_h": "2880x1800", "refresh_hz": "120Hz", "sdr_nits": "SDR 亮度 500 nits", "hdr_peak_nits": "HDR 峰值 1100 nits",
                  "dci_p3_pct": "100% DCI-P3", "certifications": "DisplayHDR True Black 600", "dimming": "DC 調光"})])
    ex2 = Extraction(relevant=True, panels=[PanelSpec(
        brand="Example B", model="Volt 14", product_type="laptop", panel_maker="BOE", panel_model="NE140QDM-NY1",
        panel_tech="IPS LCD", size_inch=14, res_w=2560, res_h=1600, refresh_hz=165, sdr_nits=412, srgb_pct=98,
        evidence={"panel_maker": "BOE", "panel_model": "NE140QDM-NY1", "panel_tech": "IPS panel", "size_inch": "14-inch",
                  "res_w": "2560x1600", "res_h": "2560x1600", "refresh_hz": "165Hz", "sdr_nits": "412 nits", "srgb_pct": "98% sRGB"})])
    ex3 = Extraction(relevant=True, is_rumor=True, panels=[PanelSpec(
        brand="範例品牌 C", model="新一代 AIO", product_type="aio", panel_tech="Mini LED LCD", size_inch=27,
        res_w=3840, res_h=2160, mini_led_zones=1152, refresh_hz=144,
        evidence={"panel_tech": "Mini LED", "size_inch": "27 吋", "res_w": "3840x2160", "res_h": "3840x2160",
                  "mini_led_zones": "1152 區", "refresh_hz": "144Hz"})])
    ex4 = Extraction(relevant=True, platform_techs=[PlatformTech(
        vendor="Intel", tech_name="（範例）低刷新省電功能", category="power", event_type="announcement",
        affected_specs=["refresh_hz", "low_power_tech"], summary="範例：相容的 OLED 筆電靜態畫面可降到 1Hz，延長續航。",
        evidence={"tech_name": "drop the refresh rate to 1Hz"})])
    ex5 = Extraction(relevant=True, fabs=[PanelFab(
        maker="範例面板廠", generation="G8.6", technology="IT OLED", application="筆電、平板",
        capacity_sheets_per_month=32000, status="trial", milestone="預計明年量產",
        summary="範例：G8.6 IT OLED 產線開始試產，月產能 32,000 片。", evidence={"generation": "第 8.6 代 IT OLED 產線"})])

    from .extract import postprocess
    out = []
    for art, ex in [(a1, ex1), (a2, ex2), (a3, ex3), (a4, ex4), (a5, ex5)]:
        ex, flags = postprocess(art, copy.deepcopy(ex))
        out.append((art, ex, flags))
    return out


def run_demo(cfg: dict) -> Path:
    out_dir = Path(cfg["report"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "demo.sqlite"
    if db_path.exists():
        db_path.unlink()
    db = DB(db_path)
    verify.ingest(db, sample_results())
    demo_cfg = copy.deepcopy(cfg)
    demo_cfg["report"]["subject_prefix"] = "[顯示器情報・範例]"
    ctx, html, xlsx, ids = build_report(db, demo_cfg, llm=None)
    db.close()
    return out_dir / f"report-{ctx['date']}.html"
