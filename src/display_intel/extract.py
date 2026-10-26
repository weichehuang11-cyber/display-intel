"""萃取層：把多語言文章轉成標準規格欄位。

- 一次送多篇文章（省免費額度），每篇有編號
- 每個數值都要附原文片段，回來後由程式逐字比對，對不上就刪掉
- 單篇或單批失敗不影響其他文章
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Callable

from pydantic import ValidationError

from . import derive
from .derive import clean_spec
from .llm import LLM, BudgetExceeded, OutputTruncated
from .schema import Extraction, PanelFab, PanelSpec, PlatformTech, RawArticle

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是筆電顯示器產業分析師，負責從新聞中萃取結構化規格。只輸出 JSON，不要任何其他文字。

規則：
1. 只寫文章明確提到的資訊，沒寫就省略該欄位，絕不推測或用常識補。
2. 每個規格欄位都要在 evidence 中放「逐字複製自文章」的短片段（5～40 字），例如 "evidence": {"refresh_hz": "240Hz OLED panel"}。
3. 同一機型有多種螢幕選配，每種各列一筆 panel。
4. 只收 Notebook、Laptop、AIO 內建螢幕；外接顯示器、手機、平板、電視不收（但面板廠產線消息可收）。
5. 爆料、傳聞、"reportedly"、"leak"、"据传"、"傳出" 等未證實內容，is_rumor 設為 true。
6. summary 類欄位用繁體中文，一到兩句。
7. 數值欄位只填數字（不含單位）；解析度拆成 res_w、res_h。
8. touch（觸控）：文章明確寫支援觸控（touchscreen、touch display、觸控螢幕）填 true；明確寫不支援（non-touch、非觸控）填 false；兩種都要附原文。沒提到就省略，不要猜。
   stylus（觸控筆）：有寫支援的觸控筆或協定（例如 MPP 2.0、USI、Active Pen）就填。
12. 平台欄位：cpu_platform 填處理器型號；gpu 填獨立顯示卡（只有內顯可省略）；gpu_switching 填 MUX、Advanced Optimus 等顯示切換方式；
    platform_certifications 填 Intel Evo、Copilot+ PC、NVIDIA Studio、AMD Advantage 等認證。一樣要附原文，沒寫就省略。
13. display_power_tech：平台顯示節能技術，保留文章寫的版本號，例如 LRR 1.0／LRR 2.0（Low Refresh Rate）、UBRR、
    DPST（Display Power Saving Technology，含版本）、OPST、PSR／PSR2（Panel Self Refresh）、Panel Replay、DRRS／SDRRS、AMD Vari-Bright。要附原文。
9. panels 只收「有品牌且有具體機型名稱」的產品；只說「某品牌新筆電」而沒有機型名稱的，不要列。
10. panel_maker 只填面板製造商（如 BOE、Samsung Display、LG Display、AUO、Innolux、CSOT、Sharp），不可填筆電品牌。
11. panel_model 只填面板料號（如 NE160QDM-NY1、ATNA60CL10、BOE0AAE）。
    品牌的螢幕行銷名稱（如 ASUS Lumina OLED、Lenovo PureSight、Dell InfinityEdge）填在 display_marketing_name，
    不可填進 panel_maker 或 panel_model；行銷名稱不代表面板製造商。
14. surface：照原文寫表面處理（例如 anti-glare、AGLR、anti-reflection coating、glossy、BrightView、霧面），不要改寫或翻譯。
16. launch_period：文章明確寫出發表或上市時間才填，格式 YYYY-MM 或 YYYY Qn（例如 2026-01、2026 Q3），要附原文；不要用文章日期猜。
15. 色域：文章寫哪個標準就填哪個（DCI-P3、sRGB、Adobe RGB、NTSC 各自獨立），不要換算；response_ms 填文章寫的反應時間數字。
17. 光學／結構／機構／介面（文章有寫才填，要附原文）：reflectance_pct 反射率%、haze_pct 霧度%、uniformity_pct 亮度均勻度%、
    viewing_angle 可視角、backplane 背板（a-Si／Oxide／LTPS／LTPO）、color_depth_desc 色深原文（10-bit、8-bit+FRC）、
    touch_structure 觸控結構（On-cell、In-cell、OGS）、bezel 邊框寬度原文、panel_thickness_mm 面板厚度、
    camera_cutout 鏡頭位置（上邊框／凹口／開孔）、edp_version eDP 版本、vrr_min_hz 可變刷新最低值、panel_power_w 面板功耗 W。
18. price／price_currency：文章寫出的上市價格或起價（starting at、起售价、起價、から）才填，只填數字（例如 1299），
    幣別填 ISO 代碼（USD、TWD、CNY、EUR、JPY、GBP、KRW、INR）；有多個價格時填這個螢幕選配的價格，分不出來就填最低起價。
    evidence 的 price 要包含金額與幣別符號原文（例如 "starting at $1,299"）。折扣價、二手價、傳聞價不要填。"""

FIELD_GUIDE = """輸出格式：
{"results": [ { "article_id": 1, "relevant": true/false, "is_rumor": true/false,
  "panels": [ {
    "brand","model","product_type"(laptop|notebook|aio),
    "panel_maker","panel_model","panel_tech"(IPS LCD|VA LCD|Mini LED LCD|OLED|Tandem OLED|其他),"display_marketing_name",
    "size_inch","res_w","res_h","surface",
    "sdr_nits","hdr_peak_nits","contrast","srgb_pct","dci_p3_pct","adobe_rgb_pct","ntsc_pct","color_depth_bit","delta_e",
    "hdr_formats":[],"certifications":[],
    "refresh_hz","vrr_range","response_ms","vrr_tech":[],
    "backlight","mini_led_zones","dimming","pwm_hz",
    "low_power_tech":[],"eye_care":[],"touch"(true/false),"stylus","glass","screen_to_body_pct","form_factor",
    "cpu_platform","gpu","gpu_switching","platform_certifications":[],"display_power_tech":[],"launch_period","price","price_currency",
    "reflectance_pct","haze_pct","uniformity_pct","viewing_angle","backplane","color_depth_desc","touch_structure",
    "bezel","panel_thickness_mm","camera_cutout","edp_version","vrr_min_hz","panel_power_w",
    "evidence": {"欄位名": "原文片段"} } ],
  "platform_techs": [ {"vendor","tech_name",
    "category"(sync|power|image|interface|certification|other),
    "event_type"(announcement|spec_update|certification|driver|patent|rumor|other),
    "affected_specs":[面板欄位名],"platforms","summary","evidence":{} } ],
  "fabs": [ {"maker","fab_site","generation","technology","application","capacity_sheets_per_month",
    "status"(planned|construction|equipment|trial|mass_production|shutdown),"milestone","investment","summary","evidence":{} } ],
  "industry_note": "與面板產業有關但不屬上述三類時的一句摘要，否則省略"
} ] }
不相關的文章只需回傳 {"article_id": N, "relevant": false}。"""


LONG_FORM_TYPES = {"review", "official"}


def article_limit(a: RawArticle, max_chars_per_article: int, max_chars_per_review: int | None = None) -> int:
    """評測與官方規格頁的規格表常在後段，給較多字數。"""
    if max_chars_per_review and a.source_type in LONG_FORM_TYPES:
        return max(max_chars_per_review, max_chars_per_article)
    return max_chars_per_article


def make_batches(articles: list[RawArticle], max_chars_per_call: int, max_chars_per_article: int,
                 max_chars_per_review: int | None = None) -> list[list[RawArticle]]:
    """依字數預算把文章分批。"""
    batches, cur, size = [], [], 0
    for a in articles:
        n = min(len(a.text), article_limit(a, max_chars_per_article, max_chars_per_review)) + 80
        if cur and size + n > max_chars_per_call:
            batches.append(cur)
            cur, size = [], 0
        cur.append(a)
        size += n
    if cur:
        batches.append(cur)
    return batches


def _render_batch(batch: list[RawArticle], max_chars: int, max_chars_review: int | None = None,
                  focus: str | None = None) -> str:
    parts = [FIELD_GUIDE]
    if focus:
        parts.append(f"\n本批文章只需萃取「{focus}」這款機型的螢幕規格，其他機型一律忽略；"
                     "同一機型有多種螢幕選配時各列一筆。platform_techs、fabs 不用填。")
    parts.append("\n以下是文章：\n")
    for i, a in enumerate(batch, 1):
        date = a.published.strftime("%Y-%m-%d") if a.published else "未知"
        limit = article_limit(a, max_chars, max_chars_review)
        parts.append(f"=== article_id: {i} | 來源: {a.source} | 日期: {date} ===\n{a.text[:limit]}\n")
    return "\n".join(parts)


def _salvage(item: dict) -> Extraction:
    """嚴格驗證失敗時，逐項保留能通過驗證的部分。"""
    try:
        return Extraction.model_validate(item)
    except ValidationError:
        pass

    def keep(model, rows):
        out = []
        for r in rows or []:
            try:
                out.append(model.model_validate(r))
            except ValidationError as exc:
                log.debug("捨棄無效項目：%s", exc.errors()[:1])
        return out

    return Extraction(relevant=bool(item.get("relevant", True)), is_rumor=bool(item.get("is_rumor", False)),
                      panels=keep(PanelSpec, item.get("panels")),
                      platform_techs=keep(PlatformTech, item.get("platform_techs")),
                      fabs=keep(PanelFab, item.get("fabs")),
                      industry_note=item.get("industry_note") if isinstance(item.get("industry_note"), str) else None)


def _check_evidence_generic(evidence: dict, text: str) -> dict:
    from .derive import quote_in_text
    return {k: v for k, v in (evidence or {}).items() if isinstance(v, str) and quote_in_text(v, text)}


GENERIC_MODELS = {"laptop", "laptops", "notebook", "notebooks", "aio", "all-in-one", "pc", "computer",
                  "筆電", "笔记本", "筆記型電腦", "一體機", "一体机", "新款", "新機", "新机", "ノートpc"}


def is_identifiable(spec: PanelSpec) -> bool:
    """品牌與具體機型名稱都必須存在（機型不能只是「Laptop」這類泛稱）。"""
    model = (spec.model or "").strip().lower()
    return bool(spec.brand and spec.brand.strip() and model and model not in GENERIC_MODELS and len(model) >= 2)


BRAND_CANON = {b.lower(): b for b in [
    "ASUS", "Acer", "Apple", "Dell", "HP", "Lenovo", "MSI", "Samsung", "Microsoft", "Razer", "LG", "Gigabyte",
    "Huawei", "Xiaomi", "Honor", "Framework", "Alienware", "Thunderobot", "Machenike", "Mechrevo", "Colorful",
    "Medion", "Dynabook", "Fujitsu", "VAIO", "Panasonic", "Google", "Infinix", "Tecno", "Chuwi"]}


BRAND_ALIASES = {"机械革命": "Mechrevo", "機械革命": "Mechrevo", "lg electronics": "LG", "樂金": "LG",
                 "华硕": "ASUS", "華碩": "ASUS", "联想": "Lenovo", "聯想": "Lenovo", "惠普": "HP", "戴尔": "Dell", "戴爾": "Dell",
                 "宏碁": "Acer", "微星": "MSI", "苹果": "Apple", "蘋果": "Apple", "华为": "Huawei", "華為": "Huawei",
                 "小米": "Xiaomi", "荣耀": "Honor", "榮耀": "Honor", "雷神": "Thunderobot", "机械师": "Machenike"}


def normalize_identity(spec: PanelSpec) -> PanelSpec:
    """統一品牌大小寫（Asus → ASUS），並去掉機型名稱開頭重複的品牌（Framework Framework Laptop 12）。"""
    data = spec.model_dump()
    brand = (spec.brand or "").strip()
    model0 = (spec.model or "").strip()
    if model0 and brand.lower().endswith(" " + model0.lower()):
        brand = brand[: -len(model0)].strip()  # 品牌欄重複了機型名稱，例如「Dell XPS 13」＋「XPS 13」
    brand = BRAND_ALIASES.get(brand.lower(), BRAND_ALIASES.get(brand, brand))
    first = brand.split()[0].lower() if brand else ""
    if brand.lower() in BRAND_CANON:
        brand = BRAND_CANON[brand.lower()]
    elif first in BRAND_CANON:  # 例如「Dell Alienware」保留後半，只統一大小寫
        brand = BRAND_CANON[first] + brand[len(first):]
    model = (spec.model or "").strip()
    if brand and model.lower().startswith(brand.lower() + " "):
        model = model[len(brand) + 1:].strip()
    for alias, canon in BRAND_ALIASES.items():  # 機型開頭重複中文品牌名，例如「机械革命 蛟龙16」
        if canon == brand and model.startswith(alias):
            model = model[len(alias):].strip()
    data["brand"], data["model"] = brand or None, model or None
    # 平台名稱去掉 ®™ 等符號，讓報告好讀（例如「Intel® Core™ Ultra 9 Processor 386H」→「Intel Core Ultra 9 386H」）
    for f in ("cpu_platform", "gpu"):
        if data.get(f):
            v = re.sub(r"[®™©]|\(R\)|\(TM\)", "", data[f])
            v = re.sub(r"\s+(Processor|Laptop GPU|GPU)\b", lambda m: "" if m.group(1) == "Processor" else " " + m.group(1), v)
            data[f] = re.sub(r"\s+", " ", v).strip()
    return PanelSpec(**data)


def fix_panel_source(spec: PanelSpec) -> tuple[PanelSpec, list[str]]:
    """面板來源只收真正的面板製造商與料號。

    - 面板廠寫成筆電品牌或行銷名稱（例如「ASUS Lumina Pro OLED」）→ 移到 display_marketing_name
    - 認不得的面板廠名稱 → 移除（只收已知面板廠，避免把行銷名稱誤當成製造商）
    - 面板型號看起來是行銷名稱 → 移到 display_marketing_name；EDID 識別碼（例如 BOE0AAE）保留
    """
    from .panel_decode import KNOWN_MAKERS, canonical_maker, decode
    flags = []
    data = spec.model_dump()
    brand = (spec.brand or "").strip().lower()
    marketing = data.get("display_marketing_name")

    def to_marketing(value: str, why: str):
        nonlocal marketing
        flags.append(f"{why}，改列為行銷名稱：{value}")
        if not marketing:
            marketing = value
            if spec.evidence.get("panel_maker") or spec.evidence.get("panel_model"):
                data["evidence"]["display_marketing_name"] = spec.evidence.get("panel_maker") or spec.evidence.get("panel_model")

    maker_raw = (spec.panel_maker or "").strip()
    if maker_raw:
        canon = canonical_maker(maker_raw)
        words = maker_raw.lower().split()
        if brand and maker_raw.lower() == brand:
            # 只寫品牌名（例如 Samsung 筆電寫「Samsung」）無法判斷是不是面板廠，不收
            data["panel_maker"] = None
            flags.append(f"面板廠只寫了筆電品牌，無法確認，已移除：{maker_raw}")
        elif canon in KNOWN_MAKERS:
            data["panel_maker"] = canon  # 例如 LG 筆電用 LG Display 面板是合理的
        elif brand and brand in words:
            data["panel_maker"] = None
            to_marketing(maker_raw, "面板廠寫成筆電品牌")
        else:
            data["panel_maker"] = None
            flags.append(f"不是已知面板廠，已移除：{maker_raw}")
    pm = (spec.panel_model or "").strip()
    if pm and pm[0].isdigit() and not decode(pm):
        # 面板料號都是英文字母開頭（NE、B、N、LP、ATNA…）；數字開頭的通常是筆電料號
        data["panel_model"] = None
        flags.append(f"面板型號看起來是筆電料號，已移除：{pm}")
        pm = ""
    looks_like_part_no = bool(re.search(r"[A-Za-z]{1,4}\d{2,}", pm.replace(" ", "")))
    if pm and not decode(pm) and (not looks_like_part_no or len(pm.split()) >= 2):
        data["panel_model"] = None
        to_marketing(pm, "面板型號看起來是行銷名稱")
    data["display_marketing_name"] = marketing
    for f in ("panel_maker", "panel_model"):
        if data.get(f) is None:
            data["evidence"].pop(f, None)
    if data.get("display_power_tech"):
        from .ihv import normalize_power_tech
        data["display_power_tech"] = normalize_power_tech(data["display_power_tech"]) or None
    return PanelSpec(**data), flags


def postprocess(article: RawArticle, ex: Extraction) -> tuple[Extraction, list[str]]:
    """原文依據檢查與合理性檢查。"""
    flags: list[str] = []
    text = article.text
    panels = []
    deal_page = bool(re.search(r"/deals?/|\bdeals?\b|cyber monday|black friday|prime day|特價|特价|促銷|促销",
                               f"{article.url} {article.title}", re.I))
    for p in ex.panels:
        why = derive.non_laptop_reason(p, article.title)
        if why:
            flags.append(f"不是筆電，未收錄（{why}）：{p.brand or ''} {p.model or ''}".strip())
            continue
        if deal_page and p.price:
            p = p.model_copy(update={"price": None, "price_currency": None})
            flags.append(f"促銷文章的價格不收：{p.brand} {p.model}")
        cleaned, f = clean_spec(p, text)
        flags += f
        cleaned, f = fix_panel_source(normalize_identity(cleaned))
        flags += f
        if is_identifiable(cleaned):  # 品牌和具體機型都要有，否則無法追蹤
            panels.append(cleaned)
        elif cleaned.brand or cleaned.model:
            flags.append(f"缺少品牌或機型，未收錄：{cleaned.brand or ''} {cleaned.model or ''}".strip())
    ex.panels = panels
    # 平台與產線消息：至少要有一段原文依據，否則視為不可靠
    ex.platform_techs = [t for t in ex.platform_techs if _check_evidence_generic(t.evidence, text)]
    ex.fabs = [f for f in ex.fabs if _check_evidence_generic(f.evidence, text)]
    for item in ex.platform_techs + ex.fabs:
        item.evidence = _check_evidence_generic(item.evidence, text)
    return ex, flags


def extract_all(articles: list[RawArticle], llm: LLM, llm_cfg: dict,
                should_stop: Callable[[], bool] = lambda: False,
                focus: str | None = None) -> tuple[list[tuple[RawArticle, Extraction, list[str]]], dict]:
    """萃取所有文章。回傳 [(文章, 萃取結果, 問題標記)] 與統計。

    focus：只萃取指定機型（規格補完時使用）。
    """
    per_article = llm_cfg.get("max_chars_per_article", 2500)
    per_review = llm_cfg.get("max_chars_per_review")
    batches = make_batches(articles, llm_cfg.get("max_chars_per_call", 6000), per_article, per_review)
    results = []
    processed: list[RawArticle] = []
    stats = {"batches": len(batches), "done_batches": 0, "failed_batches": 0, "split_batches": 0,
             "stopped": "", "errors": [], "processed": processed}
    queue = list(batches)
    bi = 0
    while queue:
        batch = queue.pop(0)
        bi += 1
        if should_stop():
            stats["stopped"] = "已到收工時間"
            break
        try:
            data = llm.complete_json(SYSTEM_PROMPT, _render_batch(batch, per_article, per_review, focus))
        except BudgetExceeded as exc:
            stats["stopped"] = str(exc)
            log.warning("停止萃取：%s", exc)
            break
        except Exception as exc:
            msg = f"{type(exc).__name__}：{str(exc)[:300]}"
            if len(stats["errors"]) < 8:
                stats["errors"].append(msg)
            # 輸出太長或 JSON 壞掉時，把這批拆成兩半重試（拆到剩一篇為止）
            if len(batch) > 1 and isinstance(exc, (OutputTruncated, ValueError)):
                half = len(batch) // 2
                queue[:0] = [batch[:half], batch[half:]]
                stats["split_batches"] += 1
                log.warning("第 %d 批輸出過長或格式錯誤，拆成兩批重試：%s", bi, msg)
                continue
            stats["failed_batches"] += 1
            log.warning("第 %d 批萃取失敗：%s", bi, msg)
            continue
        stats["done_batches"] += 1
        processed.extend(batch)
        for item in data.get("results", []):
            try:
                idx = int(item.get("article_id", 0)) - 1
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(batch):
                continue
            ex = _salvage(item)
            if not ex.relevant:
                continue
            ex, flags = postprocess(batch[idx], ex)
            results.append((batch[idx], ex, flags))
        log.info("萃取進度：已完成 %d 批，剩 %d 批（%s）", stats["done_batches"], len(queue), datetime.now().strftime("%H:%M"))
    stats["articles_with_findings"] = len(results)
    return results, stats
