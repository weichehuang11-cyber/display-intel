"""萃取層：把多語言文章轉成標準規格欄位。

- 一次送多篇文章（省免費額度），每篇有編號
- 每個數值都要附原文片段，回來後由程式逐字比對，對不上就刪掉
- 單篇或單批失敗不影響其他文章
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from pydantic import ValidationError

from .derive import clean_spec
from .llm import LLM, BudgetExceeded
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
7. 數值欄位只填數字（不含單位）；解析度拆成 res_w、res_h。"""

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


def make_batches(articles: list[RawArticle], max_chars_per_call: int, max_chars_per_article: int) -> list[list[RawArticle]]:
    """依字數預算把文章分批。"""
    batches, cur, size = [], [], 0
    for a in articles:
        n = min(len(a.text), max_chars_per_article) + 80
        if cur and size + n > max_chars_per_call:
            batches.append(cur)
            cur, size = [], 0
        cur.append(a)
        size += n
    if cur:
        batches.append(cur)
    return batches


def _render_batch(batch: list[RawArticle], max_chars: int) -> str:
    parts = [FIELD_GUIDE, "\n以下是文章：\n"]
    for i, a in enumerate(batch, 1):
        date = a.published.strftime("%Y-%m-%d") if a.published else "未知"
        parts.append(f"=== article_id: {i} | 來源: {a.source} | 日期: {date} ===\n{a.text[:max_chars]}\n")
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


def postprocess(article: RawArticle, ex: Extraction) -> tuple[Extraction, list[str]]:
    """原文依據檢查與合理性檢查。"""
    flags: list[str] = []
    text = article.text
    panels = []
    for p in ex.panels:
        cleaned, f = clean_spec(p, text)
        flags += f
        if cleaned.brand or cleaned.model:  # 連品牌機型都沒有的紀錄無法使用
            panels.append(cleaned)
    ex.panels = panels
    # 平台與產線消息：至少要有一段原文依據，否則視為不可靠
    ex.platform_techs = [t for t in ex.platform_techs if _check_evidence_generic(t.evidence, text)]
    ex.fabs = [f for f in ex.fabs if _check_evidence_generic(f.evidence, text)]
    for item in ex.platform_techs + ex.fabs:
        item.evidence = _check_evidence_generic(item.evidence, text)
    return ex, flags


def extract_all(articles: list[RawArticle], llm: LLM, llm_cfg: dict,
                should_stop: Callable[[], bool] = lambda: False) -> tuple[list[tuple[RawArticle, Extraction, list[str]]], dict]:
    """萃取所有文章。回傳 [(文章, 萃取結果, 問題標記)] 與統計。"""
    batches = make_batches(articles, llm_cfg.get("max_chars_per_call", 6000), llm_cfg.get("max_chars_per_article", 2500))
    results = []
    processed: list[RawArticle] = []
    stats = {"batches": len(batches), "done_batches": 0, "failed_batches": 0, "stopped": "", "processed": processed}
    for bi, batch in enumerate(batches, 1):
        if should_stop():
            stats["stopped"] = "已到收工時間"
            break
        try:
            data = llm.complete_json(SYSTEM_PROMPT, _render_batch(batch, llm_cfg.get("max_chars_per_article", 2500)))
        except BudgetExceeded as exc:
            stats["stopped"] = str(exc)
            log.warning("停止萃取：%s", exc)
            break
        except Exception as exc:
            stats["failed_batches"] += 1
            log.warning("第 %d 批萃取失敗：%s", bi, exc)
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
        log.info("萃取進度 %d/%d 批（%s）", bi, len(batches), datetime.now().strftime("%H:%M"))
    stats["articles_with_findings"] = len(results)
    return results, stats
