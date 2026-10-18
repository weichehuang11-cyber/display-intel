"""規格補完 agent：針對缺少重點規格的機型，主動搜尋官方規格文件與深度評測，再萃取一次。

流程（以「機型」為單位，同一款的多種螢幕選配一起處理）：
1. 挑選非傳聞、完整度未達目標（預設 10/13 ≈ 75%）、到了回訪時間的機型
   回訪時間：第一次立刻補查，之後在上市第 14、35 天再查（配合深度評測出現的時間）
2. 每款搜尋 2 次：
   - 深度評測（Notebookcheck 等）：面板型號、實測亮度、色域、ΔE、反應時間、PWM
   - 官方規格文件（品牌官網、Lenovo PSREF、HP 規格表）：亮度、色域、表面處理、觸控
3. 只保留真的提到這款機型的頁面；AI 只萃取這一款的規格（focus 模式），一樣逐字比對原文
4. 已知面板型號時，再查一次面板原廠規格，補上空白欄位（標示「面板原廠規格」）
5. Tavily 月用量達上限（預設 850）時自動停止，保留額度給臨時需求
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable
from urllib.parse import urlsplit

import httpx

from . import derive, verify
from .analytics import completeness, missing_fields
from .collect import UA, canonical_url
from .config import env
from .db import DB, utcnow
from .extract import extract_all
from .llm import LLM, BudgetExceeded
from .schema import Confidence, FieldSource, PanelRecord, PanelSpec, RawArticle

log = logging.getLogger(__name__)

# 品牌官方網域（來自這些網域的頁面視為官方，可信度 A）
BRAND_DOMAINS = {
    "asus": ["asus.com"], "lenovo": ["lenovo.com", "psref.lenovo.com"], "hp": ["hp.com"], "dell": ["dell.com"],
    "alienware": ["dell.com", "alienware.com"], "acer": ["acer.com"], "msi": ["msi.com"],
    "apple": ["apple.com"], "samsung": ["samsung.com"], "microsoft": ["microsoft.com"],
    "razer": ["razer.com"], "lg": ["lg.com"], "gigabyte": ["gigabyte.com"], "framework": ["frame.work"],
    "huawei": ["huawei.com", "consumer.huawei.com"], "xiaomi": ["mi.com", "xiaomi.com"], "honor": ["honor.com"],
}
# 深度評測網站（常列出面板型號與實測數據）
REVIEW_DOMAINS = ["notebookcheck.net", "notebookcheck.org", "notebookcheck.com", "laptopmedia.com",
                  "ultrabookreview.com", "rtings.com", "tomshardware.com", "pcmag.com"]
REVIEW_HINTS = ("notebookcheck", "review", "rtings", "laptopmedia", "tomshardware", "pcmag", "評測", "评测", "レビュー")
# 面板原廠規格可以補的欄位（只填空白，不覆蓋整機資料）
PANEL_SPEC_FIELDS = ["panel_maker", "panel_tech", "sdr_nits", "hdr_peak_nits", "contrast", "srgb_pct", "dci_p3_pct",
                     "adobe_rgb_pct", "ntsc_pct", "color_depth_bit", "color_depth_desc", "response_ms", "surface",
                     "viewing_angle", "backplane", "haze_pct", "touch_structure", "panel_thickness_mm",
                     "edp_version", "panel_power_w"]


def source_type_for(url: str, title: str, brand: str | None) -> str:
    host = urlsplit(url).netloc.lower()
    b = (brand or "").lower().split()[0] if brand else ""
    if any(host.endswith(d) for d in BRAND_DOMAINS.get(b, [])):
        return "official"
    hay = (url + " " + title).lower()
    if any(h in hay for h in REVIEW_HINTS):
        return "review"
    return "media"


# ---------- 挑選要補查的機型 ----------

def model_groups(records: list[PanelRecord]) -> list[list[PanelRecord]]:
    groups: dict[tuple, list[PanelRecord]] = {}
    for r in records:
        groups.setdefault((verify._norm(r.spec.brand), verify.model_norm(r.spec)), []).append(r)
    return list(groups.values())


def group_score(group: list[PanelRecord]) -> int:
    return max(completeness(r)[0] for r in group)


def candidates(db: DB, col: dict, now=None) -> list[list[PanelRecord]]:
    """回傳要補查的機型（每款是一組螢幕選配紀錄）。"""
    now = now or utcnow()
    schedule = col.get("enrich_schedule_days", [0, 14, 35])
    target = col.get("enrich_target", 9)
    within = timedelta(days=col.get("enrich_within_days", 60))
    spacing = timedelta(days=col.get("enrich_retry_days", 6))
    out = []
    for g in model_groups([r for r in db.all_panels() if r.confidence.rank >= Confidence.C.rank]):
        first = min(r.first_seen for r in g)
        attempts = max(r.enrich_attempts for r in g)
        last = max((r.last_enriched for r in g if r.last_enriched), default=None)
        if first < now - within or attempts >= len(schedule):
            continue
        if group_score(g) >= target:  # 已達目標，把額度留給其他機型
            continue
        if now - first < timedelta(days=schedule[attempts]):  # 還沒到回訪時間
            continue
        if last and now - last < spacing:
            continue
        out.append(g)
    out.sort(key=lambda g: (group_score(g), -max(r.first_seen for r in g).timestamp()))
    return out


# ---------- 搜尋 ----------

class TavilyBudget:
    """Tavily 月用量保護：補完只能用到 monthly_cap，超過就停。"""

    def __init__(self, db: DB, monthly_cap: int):
        self.db, self.cap = db, monthly_cap

    def ok(self) -> bool:
        return self.db.usage("tavily") < self.cap

    def spend(self, n: int = 1):
        self.db.add_usage("tavily", n)


def _tavily(client: httpx.Client, budget: TavilyBudget, query: str, max_results: int = 5,
            include_domains: list[str] | None = None) -> list[dict]:
    if not budget.ok():
        raise BudgetExceeded("Tavily 本月補完額度已達上限")
    body = {"query": query, "topic": "general", "search_depth": "basic",
            "max_results": max_results, "include_raw_content": "text"}
    if include_domains:
        body["include_domains"] = include_domains
    budget.spend()
    r = client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {env('TAVILY_API_KEY')}"}, json=body)
    r.raise_for_status()
    return r.json().get("results", [])


def search_specs(client: httpx.Client, rec: PanelRecord, budget: TavilyBudget) -> list[RawArticle]:
    """深度評測＋官方規格各搜尋一次，只保留內文真的提到這款機型的頁面。"""
    name = f"{rec.spec.brand} {rec.spec.model}".strip()
    brand_key = (rec.spec.brand or "").lower().split()[0] if rec.spec.brand else ""
    searches = [(f"{name} review display panel", REVIEW_DOMAINS)]
    official = BRAND_DOMAINS.get(brand_key)
    searches.append((f"{name} specifications display touchscreen", official) if official
                    else (f"{name} specifications display nits touchscreen", None))
    target = verify.model_norm(rec.spec)
    out, seen = [], set()
    for query, domains in searches:
        try:
            items = _tavily(client, budget, query, 5, domains)
        except BudgetExceeded:
            raise
        except Exception as exc:
            log.warning("補完搜尋失敗 %s：%s", query, exc)
            continue
        for item in items:
            raw = item.get("raw_content") or ""
            text = f"{item.get('title', '')} {item.get('content', '')} {raw[:30000]}"
            url = canonical_url(item["url"])
            if url in seen or not target or target not in verify._norm(text):
                continue
            seen.add(url)
            out.append(RawArticle(
                url=url, title=item.get("title", ""), source=urlsplit(item["url"]).netloc,
                source_type=source_type_for(item["url"], item.get("title", ""), rec.spec.brand), lang="en",
                summary=item.get("content", "")[:2000], content=raw, content_level="full" if len(raw) > 800 else "snippet"))
    out.sort(key=lambda a: {"official": 0, "review": 1}.get(a.source_type, 2))
    return out[:4]


# ---------- 面板型號（原文掃描＋專門搜尋）----------
# 評測網站的規格區通常寫出面板型號或 EDID 識別碼（例如「BOE NE160QDM-NZ1, IPS」「BOE0B8E」）
PANEL_HUNT_DOMAINS = ["notebookcheck.net", "notebookcheck.org", "notebookcheck.com", "laptopmedia.com",
                      "ultrabookreview.com", "nanoreview.net"]


def assign_panel_ids(db: DB, group: list[PanelRecord], pages: list[RawArticle]) -> list[str]:
    """從網頁原文找面板型號，填到缺型號的螢幕選配（不經 AI，原文片段即為依據）。

    只在對應明確時填入：面板型號推算的尺寸要和選配相符；EDID 識別碼（無尺寸）只在只有一個選配缺型號、
    而且頁面上只有一個識別碼時使用。回傳填入說明。
    """
    from .panel_decode import find_panel_ids
    done = []
    missing = [r for r in group if not r.spec.panel_model and r.source_of("panel_model").kind != "manual"]
    for page in pages:
        if not missing:
            break
        text = f"{page.title}\n{page.summary}\n{page.content}"
        ids = find_panel_ids(text)
        if not ids:
            continue
        for rec in list(missing):
            size = rec.spec.size_inch or rec.size_inferred
            if size:
                cands = [i for i in ids if i["size_inch"] and abs(i["size_inch"] - size) <= 0.6]
            else:
                cands = []
            if not cands and len(missing) == 1 and len(ids) == 1 and ids[0]["size_inch"] is None:
                cands = ids  # EDID 識別碼
            if len({c["id"] for c in cands}) != 1:
                continue  # 沒有或有多個候選（例如同尺寸有兩種面板），不猜
            c = cands[0]
            if rec.spec.panel_maker and rec.spec.panel_maker != c["maker"]:
                continue  # 和文章寫的面板廠矛盾，不填
            data = rec.spec.model_dump()
            data["panel_model"] = c["id"]
            data["evidence"]["panel_model"] = c["quote"]
            rec.spec = PanelSpec(**data)
            conf = verify.confidence_for(page, False)
            rec.field_sources["panel_model"] = FieldSource(url=page.url, source=page.source, conf=conf, at=utcnow())
            rec.last_updated = utcnow()
            rec = verify._derived(rec)
            db.put_panel(rec)
            db.add_event("panel", "updated", {"record": rec.model_dump(mode="json"), "changes": {"panel_model": [None, c["id"]]}},
                         conf.value, page.url, page.source)
            missing.remove(next(m for m in missing if m.key == rec.key))
            done.append(f"{rec.spec.brand} {rec.spec.model}：面板 {c['id']}（{c['maker']}）")
    return done


def hunt_panel(client: httpx.Client, rec: PanelRecord, budget: TavilyBudget) -> list[RawArticle]:
    """缺面板型號時，專門到評測網站搜尋一次（只掃描原文，不花 AI 額度）。"""
    size = rec.spec.size_inch or rec.size_inferred
    query = f"{rec.spec.brand} {rec.spec.model} {f'{size:g} inch ' if size else ''}display panel"
    target = verify.model_norm(rec.spec)
    out = []
    for item in _tavily(client, budget, query, 5, PANEL_HUNT_DOMAINS):
        raw = item.get("raw_content") or ""
        if not target or target not in verify._norm(f"{item.get('title', '')} {item.get('content', '')} {raw[:30000]}"):
            continue
        out.append(RawArticle(url=canonical_url(item["url"]), title=item.get("title", ""), source=urlsplit(item["url"]).netloc,
                              source_type="review", summary=item.get("content", "")[:2000], content=raw,
                              content_level="full" if len(raw) > 800 else "snippet"))
    return out


# ---------- 面板型號反查 ----------

PANEL_PROMPT = """你是顯示器面板規格分析師。從文章中找出面板「{model}」的原廠規格，只輸出 JSON：
{{"panel": {{"panel_maker": "", "panel_tech": "", "sdr_nits": 數字, "hdr_peak_nits": 數字, "contrast": "",
"srgb_pct": 數字, "dci_p3_pct": 數字, "adobe_rgb_pct": 數字, "ntsc_pct": 數字, "color_depth_bit": 數字,
"color_depth_desc": "", "response_ms": 數字, "surface": "", "viewing_angle": "", "backplane": "", "haze_pct": 數字,
"touch_structure": "", "panel_thickness_mm": 數字, "edp_version": "", "panel_power_w": 數字,
"evidence": {{"欄位名": "逐字複製自文章的片段"}}}}}}
規則：只寫文章中屬於這片面板的數值，沒有就省略；每個欄位都要附 evidence；找不到這片面板就回傳 {{"panel": null}}。"""


def lookup_panel(client: httpx.Client, llm: LLM, budget: TavilyBudget, panel_model: str) -> tuple[dict, str]:
    """查面板原廠規格。回傳（欄位 → 值, 來源網址）。"""
    items = _tavily(client, budget, f"{panel_model} panel specifications", 4)
    key = verify._norm(panel_model)
    for item in items:
        raw = item.get("raw_content") or item.get("content") or ""
        if key not in verify._norm(raw):
            continue
        text = raw[:8000]
        data = llm.complete_json(PANEL_PROMPT.format(model=panel_model), text)
        panel = data.get("panel") if isinstance(data, dict) else None
        if not panel:
            continue
        fields = {k: v for k, v in panel.items() if k in PANEL_SPEC_FIELDS or k == "evidence"}
        try:
            spec = PanelSpec(**fields)
        except Exception:
            continue
        spec, _ = derive.clean_spec(spec, text)
        values = {f: getattr(spec, f) for f in PANEL_SPEC_FIELDS if getattr(spec, f) not in (None, "", [])}
        if values:
            return values, canonical_url(item["url"])
    return {}, ""


def apply_panel_specs(db: DB, rec: PanelRecord, values: dict, url: str) -> dict:
    """面板原廠規格只填空白欄位，並記錄是哪些欄位。回傳變更。"""
    from .panel_decode import canonical_maker
    data = rec.spec.model_dump()
    changes = {}
    for f, v in values.items():
        if data.get(f) in (None, "", []):
            data[f] = canonical_maker(v) if f == "panel_maker" else v
            changes[f] = [None, data[f]]
            rec.field_sources[f] = FieldSource(url=url, source="面板原廠規格", conf=Confidence.C, at=utcnow(),
                                               kind="panel_datasheet")
    if changes:
        rec.spec = PanelSpec(**data)
        rec.panel_spec_fields = sorted(set(rec.panel_spec_fields) | set(changes))
        note = f"部分規格來自面板 {rec.spec.panel_model} 原廠規格（{url}）"
        if note not in rec.flags:
            rec.flags.append(note)
        rec.last_updated = utcnow()
        db.put_panel(verify._derived(rec))
    return changes


# ---------- 關注機型 ----------

def watch_items(cfg: dict) -> list[dict]:
    out = []
    for it in cfg.get("watchlist") or []:
        if not isinstance(it, dict) or not it.get("brand") or not it.get("model"):
            continue
        brand, model = str(it["brand"]).strip(), str(it["model"]).strip()
        names = [model] + [str(a) for a in it.get("aliases") or []]
        out.append({"name": f"{brand} {model}", "brand": brand, "model": model,
                    "keys": [verify._norm(n) for n in names if verify._norm(n)], "lang": it.get("lang", "en")})
    return out


def watch_matches(item: dict, spec: PanelSpec) -> bool:
    """規格是否屬於這個關注機型：品牌相同，且機型名稱包含系列名稱或其他寫法。"""
    b = verify._norm(spec.brand)
    if not b or not (b.startswith(verify._norm(item["brand"])) or verify._norm(item["brand"]).startswith(b)):
        return False
    m = verify.model_norm(spec)
    return any(k in m for k in item["keys"])


def watch_due(db: DB, cfg: dict, now=None) -> list[dict]:
    now = now or utcnow()
    every = timedelta(days=(cfg.get("collection") or {}).get("watch_refresh_days", 30))
    due = []
    for it in watch_items(cfg):
        last = db.watch_last_checked(it["name"])
        if last is None or now - last >= every:
            due.append((last is not None, last or now, it))
    due.sort(key=lambda x: (x[0], x[1]))  # 從沒查過的優先，其次是最久沒查的
    return [it for _, _, it in due]


def search_watch(client: httpx.Client, item: dict, budget: TavilyBudget) -> list[RawArticle]:
    """關注機型搜尋 2 次：評測＋官方（中文機型改用中文查詢）。只保留內文提到該系列的頁面。"""
    brand_key = item["brand"].lower()
    if item["lang"] == "zh":
        zh_brand = {"mechrevo": "机械革命"}.get(brand_key, item["brand"])
        searches = [(f"{zh_brand} {item['model']} 屏幕 参数 评测", None), (f"{zh_brand} {item['model']} 笔记本 规格 刷新率", None)]
    else:
        searches = [(f"{item['name']} review display panel", REVIEW_DOMAINS),
                    (f"{item['name']} specifications display touchscreen", BRAND_DOMAINS.get(brand_key))]
    out, seen = [], set()
    for query, domains in searches:
        try:
            items = _tavily(client, budget, query, 5, domains)
        except BudgetExceeded:
            raise
        except Exception as exc:
            log.warning("關注機型搜尋失敗 %s：%s", query, exc)
            continue
        for r in items:
            raw = r.get("raw_content") or ""
            text = verify._norm(f"{r.get('title', '')} {r.get('content', '')} {raw[:30000]}")
            url = canonical_url(r["url"])
            if url in seen or not any(k in text for k in item["keys"]):
                continue
            seen.add(url)
            out.append(RawArticle(url=url, title=r.get("title", ""), source=urlsplit(r["url"]).netloc,
                                  source_type=source_type_for(r["url"], r.get("title", ""), item["brand"]),
                                  lang="zh-CN" if item["lang"] == "zh" else "en", summary=r.get("content", "")[:2000],
                                  content=raw, content_level="full" if len(raw) > 800 else "snippet"))
    out.sort(key=lambda a: {"official": 0, "review": 1}.get(a.source_type, 2))
    return out[:4]


def tag_watch_records(db: DB, item: dict) -> int:
    """標記屬於這個關注機型的紀錄。剛建檔的紀錄視為已做過第一次補查，避免同一次執行重複搜尋。"""
    n = 0
    for r in db.all_panels():
        if not watch_matches(item, r.spec):
            continue
        n += 1
        changed = r.watch != item["name"]
        r.watch = item["name"]
        if r.enrich_attempts == 0:
            r.enrich_attempts, r.last_enriched, changed = 1, utcnow(), True
        if changed:
            db.put_panel(r)
    return n


def seed_watchlist(db: DB, cfg: dict, llm: LLM, client: httpx.Client, budget: TavilyBudget, limit: int,
                   should_stop: Callable[[], bool], stats: dict):
    """幫到期的關注機型建檔或回查。"""
    seen = db.seen_urls()
    for item in watch_due(db, cfg)[:limit]:
        if should_stop():
            stats["stopped"] = "已到收工時間"
            return
        tag_watch_records(db, item)  # 已經因新聞收錄的機型，先標記
        pages = [p for p in search_watch(client, item, budget) if p.url not in seen]
        stats["watch_checked"] += 1
        found = 0
        if pages:
            results, ex_stats = extract_all(pages, llm, cfg["llm"], should_stop, focus=item["name"])
            kept = []
            for article, ex, flags in results:
                ex.panels = [p for p in ex.panels if watch_matches(item, p)]
                ex.platform_techs, ex.fabs, ex.industry_note = [], [], None
                if ex.panels:
                    kept.append((article, ex, flags))
                    found += len(ex.panels)
            verify.ingest(db, kept, tag="watch")
            for p in ex_stats.get("processed", []):
                db.add_article(p.url, p.title, p.source)
                seen.add(p.url)
            if ex_stats.get("stopped"):
                stats["stopped"] = ex_stats["stopped"]
                db.mark_watch_checked(item["name"], found)
                return
        tagged = tag_watch_records(db, item)
        db.mark_watch_checked(item["name"], found)
        if found:
            stats["watch_found"] += 1
            stats["details"].append(f"關注機型 {item['name']}：找到 {found} 筆螢幕規格")
        elif not tagged:
            stats["details"].append(f"關注機型 {item['name']}：這次沒找到規格頁")


# ---------- 主流程 ----------

def enrich(db: DB, cfg: dict, llm: LLM, limit: int, should_stop: Callable[[], bool] = lambda: False) -> dict:
    """補完最多 limit 款機型。回傳統計。"""
    col = cfg.get("collection") or {}
    stats = {"candidates": 0, "searched": 0, "tavily": 0, "pages": 0, "improved": 0, "fields_added": 0,
             "panel_lookups": 0, "panel_ids": 0, "panel_hunts": 0, "watch_checked": 0, "watch_found": 0, "stopped": "",
             "details": []}
    if limit <= 0 or not env("TAVILY_API_KEY"):
        stats["stopped"] = "未設定 TAVILY_API_KEY 或補完數量為 0"
        return stats
    budget = TavilyBudget(db, col.get("enrich_monthly_cap", 850))
    used_before = db.usage("tavily")
    with httpx.Client(timeout=httpx.Timeout(40.0), headers={"User-Agent": UA}) as client:
        try:
            # 關注機型先建檔（新建的紀錄接著會進入下方的一般補完流程）
            watch_limit = col.get("watch_per_run", 5) * (2 if (cfg.get("run") or {}).get("profile") in ("full", "enrich") else 1)
            if watch_items(cfg) and watch_limit:
                seed_watchlist(db, cfg, llm, client, budget, watch_limit, should_stop, stats)
            todo = candidates(db, col)
            stats["candidates"] = len(todo)
            seen = db.seen_urls()
            for group in todo[:limit]:
                if should_stop():
                    stats["stopped"] = "已到收工時間"
                    break
                rep = max(group, key=lambda r: completeness(r)[0])
                before = group_score(group)
                name = f"{rep.spec.brand} {rep.spec.model}"
                for r in group:
                    r.enrich_attempts += 1
                    r.last_enriched = utcnow()
                    db.put_panel(r)
                pages = [p for p in search_specs(client, rep, budget) if p.url not in seen]
                stats["searched"] += 1
                stats["pages"] += len(pages)
                if pages:
                    results, ex_stats = extract_all(pages, llm, cfg["llm"], should_stop, focus=name)
                    target = verify.model_norm(rep.spec)
                    kept = []
                    for article, ex, flags in results:
                        ex.panels = [p for p in ex.panels if verify._norm(p.brand) == verify._norm(rep.spec.brand)
                                     and verify.model_norm(p) == target]
                        ex.platform_techs, ex.fabs, ex.industry_note = [], [], None
                        if ex.panels:
                            kept.append((article, ex, flags))
                    verify.ingest(db, kept)
                    for p in ex_stats.get("processed", []):
                        db.add_article(p.url, p.title, p.source)
                        seen.add(p.url)
                    if ex_stats.get("stopped"):
                        stats["stopped"] = ex_stats["stopped"]
                        break
                same = lambda r: (verify._norm(r.spec.brand) == verify._norm(rep.spec.brand)
                                  and verify.model_norm(r.spec) == verify.model_norm(rep.spec))
                # 面板型號：先掃描剛抓到的頁面原文；還缺的話專門到評測網站搜尋一次
                current = [r for r in db.all_panels() if same(r)]
                found = assign_panel_ids(db, current, pages)
                if col.get("panel_hunt", True) and any(not r.spec.panel_model for r in current) \
                        and not any(r.panel_hunt_done for r in current if not r.spec.panel_model):
                    for r in current:
                        if not r.spec.panel_model:
                            r.panel_hunt_done = True  # 同一款只專門搜尋一次
                            db.put_panel(r)
                    hunt = [p for p in hunt_panel(client, rep, budget) if p.url not in seen]
                    stats["panel_hunts"] += 1
                    found += assign_panel_ids(db, [r for r in db.all_panels() if same(r)], hunt)
                stats["panel_ids"] += len(found)
                stats["details"] += found
                # 面板型號反查（同一款只查一次）
                current = [r for r in db.all_panels() if same(r)]
                for r in current:
                    if r.spec.panel_model and not r.panel_lookup_done and len(missing_fields(r)) > 0:
                        r.panel_lookup_done = True
                        db.put_panel(r)
                        values, url = lookup_panel(client, llm, budget, r.spec.panel_model)
                        stats["panel_lookups"] += 1
                        changes = apply_panel_specs(db, r, values, url) if values else {}
                        if changes:
                            db.add_event("panel", "updated", {"record": r.model_dump(mode="json"), "changes": changes},
                                         r.confidence.value, url, urlsplit(url).netloc if url else "面板規格")
                after = group_score(current) if current else before
                if after > before:
                    stats["improved"] += 1
                    stats["fields_added"] += after - before
                    stats["details"].append(f"{name}：{before} → {after} 項")
        except BudgetExceeded as exc:
            stats["stopped"] = str(exc)
            log.warning("停止補完：%s", exc)
    stats["tavily"] = db.usage("tavily") - used_before
    stats["tavily_month_total"] = db.usage("tavily")
    log.info("規格補完：候選 %d 款，搜尋 %d 款（Tavily %d 次），%d 款補到新規格，共新增 %d 項；面板反查 %d 次",
             stats["candidates"], stats["searched"], stats["tavily"], stats["improved"], stats["fields_added"], stats["panel_lookups"])
    return stats
