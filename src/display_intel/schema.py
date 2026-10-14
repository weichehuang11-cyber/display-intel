"""資料結構：面板規格、平台技術動態、面板廠世代線，以及可信度等級。

欄位定義對應設計文件的「面板規格 Schema」「平台技術追蹤 Schema」「面板廠技術能力與世代線」。
所有規格欄位都是選填：文章沒寫就留空，絕不猜測。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    A = "A"  # 官方
    B = "B"  # 實測（評測、拆機）
    C = "C"  # 媒體報導
    D = "D"  # 傳聞（社群、單一來源）
    E = "E"  # 待確認（可疑或衝突，不進日報）

    @property
    def label(self) -> str:
        return {"A": "官方", "B": "實測", "C": "媒體報導", "D": "傳聞", "E": "待確認"}[self.value]

    @property
    def rank(self) -> int:
        """數字越大越可信。"""
        return {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}[self.value]


SOURCE_TYPE_CONFIDENCE = {
    "official": Confidence.A,
    "review": Confidence.B,
    "media": Confidence.C,
    "aggregator": Confidence.C,
    "social": Confidence.D,
}


class RawArticle(BaseModel):
    url: str
    title: str
    source: str
    source_type: str = "media"
    lang: str = "en"
    published: Optional[datetime] = None
    summary: str = ""
    content: str = ""
    content_level: Literal["full", "snippet"] = "snippet"
    topics: list[str] = Field(default_factory=list)  # 初篩命中的主題：panel / platform / fab

    @property
    def text(self) -> str:
        body = self.content or self.summary
        return f"{self.title}\n\n{body}"


# ---------- AI 萃取輸出 ----------

class PanelSpec(BaseModel):
    """一款機型搭配的一種面板選項。"""

    # 識別
    brand: Optional[str] = Field(None, description="品牌，例如 ASUS、Lenovo")
    model: Optional[str] = Field(None, description="機型名稱，不含螢幕描述")
    product_type: Optional[Literal["laptop", "notebook", "aio"]] = None
    regions: Optional[list[str]] = Field(None, description="上市地區")
    # 面板來源
    panel_maker: Optional[str] = Field(None, description="面板廠，例如 BOE、Samsung Display")
    panel_model: Optional[str] = Field(None, description="面板型號，例如 NE160QDM-NZ1")
    panel_tech: Optional[str] = Field(None, description="IPS LCD / VA LCD / Mini LED LCD / OLED / Tandem OLED 等")
    # 基本
    size_inch: Optional[float] = None
    res_w: Optional[int] = Field(None, description="水平像素")
    res_h: Optional[int] = Field(None, description="垂直像素")
    surface: Optional[str] = Field(None, description="霧面 / 亮面 / 抗反射")
    # 亮度與色彩
    sdr_nits: Optional[float] = None
    hdr_peak_nits: Optional[float] = None
    contrast: Optional[str] = Field(None, description="例如 1,000,000:1")
    srgb_pct: Optional[float] = None
    dci_p3_pct: Optional[float] = None
    adobe_rgb_pct: Optional[float] = None
    color_depth_bit: Optional[int] = None
    delta_e: Optional[float] = None
    hdr_formats: Optional[list[str]] = Field(None, description="HDR10、Dolby Vision 等")
    certifications: Optional[list[str]] = Field(None, description="DisplayHDR、TÜV、Pantone 等")
    # 動態
    refresh_hz: Optional[float] = None
    vrr_range: Optional[str] = Field(None, description="例如 48-240Hz")
    response_ms: Optional[float] = None
    vrr_tech: Optional[list[str]] = Field(None, description="G-SYNC、FreeSync、Adaptive Sync")
    # 背光與調光
    backlight: Optional[str] = None
    mini_led_zones: Optional[int] = None
    dimming: Optional[str] = Field(None, description="DC / PWM / 混合")
    pwm_hz: Optional[float] = None
    # 節能與健康
    low_power_tech: Optional[list[str]] = Field(None, description="LTPO、PSR、動態刷新率等")
    eye_care: Optional[list[str]] = Field(None, description="低藍光、無閃爍認證")
    # 觸控與結構
    touch: Optional[bool] = None
    stylus: Optional[str] = None
    glass: Optional[str] = None
    screen_to_body_pct: Optional[float] = None
    form_factor: Optional[str] = Field(None, description="一般 / 可折疊 / 雙螢幕 / 翻轉")
    # 平台（與 Intel / NVIDIA / AMD / Qualcomm 的搭配）
    cpu_platform: Optional[str] = Field(None, description="處理器，例如 Intel Core Ultra 7 258V、AMD Ryzen AI 9 HX 370")
    gpu: Optional[str] = Field(None, description="獨立顯示卡，例如 NVIDIA GeForce RTX 5070 Laptop；只有內顯就寫內顯型號或省略")
    gpu_switching: Optional[str] = Field(None, description="顯示切換：MUX、Advanced Optimus、Optimus、AMD Smart Access Graphics 等")
    platform_certifications: Optional[list[str]] = Field(None, description="Intel Evo、Copilot+ PC、NVIDIA Studio、AMD Advantage 等平台認證")
    display_power_tech: Optional[list[str]] = Field(None, description="平台顯示節能技術（含版本），例如 LRR 2.0、UBRR、DPST 8.0、OPST、PSR2、Panel Replay")
    # 依據：欄位名稱 → 文章原文片段（必須逐字出自文章）
    evidence: dict[str, str] = Field(default_factory=dict)


class PlatformTech(BaseModel):
    vendor: str = Field(..., description="Intel / NVIDIA / AMD / Qualcomm / 其他")
    tech_name: str
    category: Literal["sync", "power", "image", "interface", "certification", "other"]
    event_type: Literal["announcement", "spec_update", "certification", "driver", "patent", "rumor", "other"]
    affected_specs: list[str] = Field(default_factory=list, description="影響的面板規格欄位，例如 refresh_hz")
    platforms: Optional[str] = Field(None, description="支援的處理器或 GPU 世代")
    summary: str = Field(..., description="一到兩句繁體中文，說明對筆電螢幕的影響")
    evidence: dict[str, str] = Field(default_factory=dict)


class PanelFab(BaseModel):
    maker: str
    fab_site: Optional[str] = None
    generation: Optional[str] = Field(None, description="G6 / G8.6 / G10.5 等")
    technology: Optional[str] = Field(None, description="IT OLED / LCD / Mini LED 等")
    application: Optional[str] = None
    capacity_sheets_per_month: Optional[int] = None
    status: Optional[Literal["planned", "construction", "equipment", "trial", "mass_production", "shutdown"]] = None
    milestone: Optional[str] = Field(None, description="關鍵時程，例如 2026 年量產")
    investment: Optional[str] = None
    summary: str = Field(..., description="一到兩句繁體中文")
    evidence: dict[str, str] = Field(default_factory=dict)


class Extraction(BaseModel):
    """一篇文章的萃取結果。"""

    relevant: bool = Field(..., description="文章是否與筆電/AIO 螢幕、平台顯示技術或面板廠產線有關")
    is_rumor: bool = Field(False, description="內容是否為爆料、傳聞或未經證實的消息")
    panels: list[PanelSpec] = Field(default_factory=list)
    platform_techs: list[PlatformTech] = Field(default_factory=list)
    fabs: list[PanelFab] = Field(default_factory=list)
    industry_note: Optional[str] = Field(None, description="不屬於上述三類、但與面板產業相關的一句繁體中文摘要")


# ---------- 查核後的紀錄 ----------

class PanelRecord(BaseModel):
    key: str
    spec: PanelSpec
    confidence: Confidence
    source_url: str
    source_name: str
    first_seen: datetime
    last_updated: datetime
    # 衍生規格
    ppi: Optional[float] = None
    pixel_count: Optional[int] = None
    aspect_ratio: Optional[str] = None
    density_class: Optional[str] = None
    # 由面板型號推算（文章沒寫時才用，報告會標示「推算」）
    panel_maker_inferred: Optional[str] = None
    size_inferred: Optional[float] = None
    flags: list[str] = Field(default_factory=list)  # 查核發現的問題
    # 規格補完紀錄
    enrich_attempts: int = 0
    last_enriched: Optional[datetime] = None
    panel_lookup_done: bool = False
    panel_spec_fields: list[str] = Field(default_factory=list)  # 來自面板原廠規格（非整機實測）的欄位

    @property
    def maker(self) -> Optional[str]:
        """統一名稱後的面板廠（文章寫的優先，其次是型號推算）。"""
        from .panel_decode import canonical_maker
        return canonical_maker(self.spec.panel_maker) or self.panel_maker_inferred


class PanelEvent(BaseModel):
    kind: Literal["new", "updated", "rumor_confirmed"]
    record: PanelRecord
    changes: dict[str, list] = Field(default_factory=dict)  # 欄位 → [舊值, 新值]
