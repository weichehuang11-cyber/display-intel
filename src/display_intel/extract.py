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
9. panels 只收「有品牌且有具體機型名稱」的產品；只說「某品牌新筆電」而沒有機型名稱的，不要列。
10. panel_maker 只填面板製造商（如 BOE、Samsung Display、LG Display、AUO、Innolux、CSOT、Sharp），不可填筆電品牌。
11. panel_model 只填面板料號（如 NE160QDM-NY1、ATNA60CL10），行銷名稱（如 Lumina OLED、Nebula Display）不要填。"""

FIELD_GUIDE = """輸出格式：
{"results": [ { "article_id": 1, "relevant": true/false, "is_rumor": true/false,
  "panels": [ {
    "brand","model","product_type"(laptop|notebook|aio),
    "panel_maker","panel_model","panel_tech"(IPS LCD|VA LCD|Mini LED LCD|OLED|Tandem OLED|其他),
    "size_inch","res_w","res_h","surface",
    "sdr_nits","hdr_peak_nits","contrast","srgb_pct","dci_p3_pct","adobe_rgb_pct","color_depth_bit","delta_e",
    "hdr_formats":[],"certifications":[],
    "refresh_hz","vrr_range","response_ms","vrr_tech":[],
    "backlight","mini_led_zones","dimming","pwm_hz",
    "low_power_tech":[],"eye_care":[],"touch"(true/false),"stylus","glass","screen_to_body_pct","form_factor",
    "cpu_platform","gpu","gpu_switching","platform_certifications":[],
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


def normalize_identity(spec: PanelSpec) -> PanelSpec:
    """統一品牌大小寫（Asus → ASUS），並去掉機型名稱開頭重複的品牌（Framework Framework Laptop 12）。"""
    data = spec.model_dump()
    brand = (spec.brand or "").strip()
    first = brand.split()[0].lower() if brand else ""
    if brand.lower() in BRAND_CANON:
        brand = BRAND_CANON[brand.lower()]
    elif first in BRAND_CANON:  # 例如「Dell Alienware」保留後半，只統一大小寫
        brand = BRAND_CANON[first] + brand[len(first):]
    model = (spec.model or "").strip()
    if brand and model.lower().startswith(brand.lower() + " "):
        model = model[len(brand) + 1:].strip()
    data["brand"], data["model"] = brand or None, model or None
    return PanelSpec(**data)


def fix_panel_source(spec: PanelSpec) -> tuple[PanelSpec, list[str]]:
    """移除填錯的面板來源：面板廠填成筆電品牌、面板型號填成行銷名稱。"""
    from .panel_decode import canonical_maker, decode
    flags = []
    data = spec.model_dump()
    brand = (spec.brand or "").strip().lower()
    maker = (spec.panel_maker or "").strip().lower()
    if maker and brand and (maker == brand or maker in brand.split() or brand in maker.split()):
        data["panel_maker"] = None
        flags.append(f"面板廠填成筆電品牌，已移除：{spec.panel_maker}")
    pm = spec.panel_model or ""
    looks_like_part_no = bool(re.search(r"[A-Za-z]{1,4}\d{2,}", pm.replace(" ", "")))
    if pm and not decode(pm) and (not looks_like_part_no or len(pm.split()) >= 2):
        data["panel_model"] = None
        flags.append(f"面板型號看起來是行銷名稱，已移除：{pm}")
    if data.get("panel_maker"):
        data["panel_maker"] = canonical_maker(data["panel_maker"])
    for f in ("panel_maker", "panel_model"):
        if data.get(f) is None:
            data["evidence"].pop(f, None)
    return PanelSpec(**data), flags


def postprocess(article: RawArticle, ex: Extraction) -> tuple[Extraction, list[str]]:
    """原文依據檢查與合理性檢查。"""
    flags: list[str] = []
    text = article.text
    panels = []
    for p in ex.panels:
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
