"""核心邏輯測試（不需網路、不需金鑰）：python -m pytest"""

from datetime import datetime, timezone

import pytest

from display_intel import derive, verify
from display_intel.collect import canonical_url, classify, html_to_text
from display_intel.db import DB
from display_intel.extract import extract_all, make_batches
from display_intel.llm import BudgetExceeded, parse_json
from display_intel.schema import Confidence, Extraction, PanelSpec, RawArticle

KEYWORDS = {
    "device": ["laptop", "筆電"], "display": ["oled", "display", "螢幕"],
    "platform_vendor": ["intel"], "panel_maker": ["boe"], "fab": ["gen 8.6"],
}


def art(title="t", body="", source_type="media", url=None):
    return RawArticle(url=url or f"https://x.com/{abs(hash(title + body))}", title=title, source="S",
                      source_type=source_type, content=body, published=datetime.now(timezone.utc))


# ---------- 衍生規格 ----------

def test_ppi_and_aspect():
    assert derive.ppi(2560, 1600, 16) == 188.7
    assert derive.aspect_ratio(2560, 1600) == "16:10"
    assert derive.aspect_ratio(1920, 1080) == "16:9"
    assert derive.aspect_ratio(3000, 2000) == "3:2"
    assert derive.density_class(140) == "一般"
    assert derive.density_class(189) == "高"
    assert derive.density_class(260) == "極高"
    assert derive.ppi(None, 1600, 16) is None


def test_plausibility_removes_bad_values():
    spec = PanelSpec(brand="X", model="Y", size_inch=13, res_w=7680, res_h=4320, refresh_hz=2400, sdr_nits=500)
    bad = derive.plausibility_issues(spec)
    assert "refresh_hz" in bad and "ppi" in bad
    assert "sdr_nits" not in bad


def test_evidence_required():
    text = "The Foo 14 laptop has a 14-inch OLED panel at 120Hz."
    spec = PanelSpec(brand="Acme", model="Foo 14", size_inch=14, refresh_hz=120, sdr_nits=600,
                     evidence={"size_inch": "14-inch", "refresh_hz": "120Hz", "sdr_nits": "600 nits"})
    cleaned, dropped = derive.enforce_evidence(spec, text)
    assert cleaned.refresh_hz == 120 and cleaned.size_inch == 14
    assert cleaned.sdr_nits is None and "sdr_nits" in dropped  # 文章沒寫 600 nits → 刪除
    assert cleaned.brand is None  # 品牌沒出現在文中
    assert cleaned.model == "Foo 14"


def test_quote_matching_ignores_width_and_space():
    assert derive.quote_in_text("２８８０ｘ１８００", "解析度 2880x1800 OLED")
    assert derive.quote_in_text("120  Hz", "a 120 Hz panel")
    assert not derive.quote_in_text("240Hz", "a 120Hz panel")


# ---------- 蒐集 ----------

def test_classify_requires_two_keyword_groups():
    assert classify(art("New laptop with OLED display"), KEYWORDS) == ["panel"]
    assert classify(art("New laptop with better battery"), KEYWORDS) == []
    assert "fab" in classify(art("BOE Gen 8.6 line"), KEYWORDS)
    assert "platform" in classify(art("Intel display tech"), KEYWORDS)


def test_html_and_url_helpers():
    assert "Hello" in html_to_text("<html><script>x()</script><p>Hello <b>world</b></p></html>")
    assert "x()" not in html_to_text("<html><script>x()</script><p>Hello</p></html>")
    assert canonical_url("https://A.com/x/?utm_source=a&id=3") == "https://a.com/x?id=3"


# ---------- AI 回覆解析 ----------

def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('好的：{"a": [1,2]} 完成') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json("沒有 JSON")


def test_make_batches_respects_budget():
    arts = [art(str(i), "x" * 1000) for i in range(10)]
    batches = make_batches(arts, max_chars_per_call=3000, max_chars_per_article=1500)
    assert sum(len(b) for b in batches) == 10
    assert all(len(b) <= 3 for b in batches)


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_extract_maps_ids_and_drops_hallucination():
    a1 = art("Acme Foo 14 laptop OLED", "Acme Foo 14 has a 14-inch OLED panel with 120Hz refresh rate.")
    a2 = art("Weather today", "Sunny.")
    reply = {"results": [
        {"article_id": 1, "relevant": True, "panels": [
            {"brand": "Acme", "model": "Foo 14", "panel_tech": "OLED", "size_inch": 14, "refresh_hz": 240,
             "evidence": {"panel_tech": "OLED panel", "size_inch": "14-inch", "refresh_hz": "240Hz"}}]},
        {"article_id": 2, "relevant": False},
        {"article_id": 9, "relevant": True},  # 不存在的編號 → 忽略
    ]}
    results, stats = extract_all([a1, a2], FakeLLM([reply]), {"max_chars_per_call": 10000})
    assert len(results) == 1
    _, ex, flags = results[0]
    assert ex.panels[0].size_inch == 14
    assert ex.panels[0].refresh_hz is None  # 原文是 120Hz，AI 說 240Hz → 被刪除
    assert any("refresh_hz" in f for f in flags)


def test_extract_stops_when_budget_exceeded():
    arts = [art(str(i), "laptop oled " + "x" * 3000) for i in range(4)]
    llm = FakeLLM([{"results": []}, BudgetExceeded("用完")])
    results, stats = extract_all(arts, llm, {"max_chars_per_call": 3200, "max_chars_per_article": 3100})
    assert stats["done_batches"] == 1 and "用完" in stats["stopped"]


# ---------- 查核與資料庫 ----------

@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "t.sqlite")
    yield d
    d.close()


def test_merge_new_update_and_rumor_confirmed(db):
    spec = PanelSpec(brand="Acme", model="Foo 14", size_inch=14, panel_tech="OLED", res_w=2880, res_h=1800, refresh_hz=120)
    rumor = art("leak", source_type="social")
    ev = verify.merge_panel(db, spec, Confidence.D, rumor, [])
    assert ev.kind == "new" and ev.record.ppi == 242.6

    # 同樣規格再出現一次，沒有新資訊 → 不產生事件
    assert verify.merge_panel(db, spec, Confidence.D, rumor, []) is None

    # 官方公布並補上亮度 → 傳聞證實
    official = art("official", source_type="official")
    spec2 = spec.model_copy(update={"sdr_nits": 500})
    ev = verify.merge_panel(db, spec2, Confidence.A, official, [])
    assert ev.kind == "rumor_confirmed" and ev.record.confidence == Confidence.A
    assert ev.changes["sdr_nits"] == [None, 500]

    # 較低可信度的來源不能覆蓋官方數值
    spec3 = spec.model_copy(update={"refresh_hz": 144})
    assert verify.merge_panel(db, spec3, Confidence.C, art("m"), []) is None
    assert db.get_panel(verify.panel_key(spec)).spec.refresh_hz == 120


def test_dedupe_articles(db):
    a = art("ASUS unveils new OLED laptop lineup", source_type="aggregator", url="https://a/1")
    b = art("ASUS unveils new OLED laptop line-up", source_type="official", url="https://b/1")
    c = art("Completely different story", url="https://c/1")
    db.add_article("https://c/1", "Completely different story", "S")
    kept = verify.dedupe_articles([a, b, c], db)
    assert [k.url for k in kept] == ["https://b/1"]  # 保留官方來源；c 已處理過


def test_ingest_and_report(db, tmp_path):
    from display_intel.report import build_report
    a = art("Acme Foo 14 laptop", "Acme Foo 14 has a 14-inch OLED panel.", source_type="official")
    ex = Extraction(relevant=True, panels=[PanelSpec(brand="Acme", model="Foo 14", size_inch=14, panel_tech="OLED")])
    stats = verify.ingest(db, [(a, ex, [])])
    assert stats["panel_new"] == 1
    cfg = {"schedule": {"timezone": "Asia/Taipei"}, "report": {"output_dir": str(tmp_path), "subject_prefix": "[T]"}}
    ctx, html, xlsx, ids = build_report(db, cfg)
    assert "Foo 14" in html and xlsx.exists() and len(ids) == 1
    assert "新機 1" in ctx["subject"]
    db.mark_reported(ids)
    assert db.unreported_events() == []


# ---------- 免費 AI 介面（模擬伺服器，不連網） ----------

def test_gemini_request_and_429(monkeypatch):
    import httpx
    from display_intel.llm import LLM

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "5"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    llm = LLM("gemini", "gemini-3.5-flash-lite", max_calls=5)
    llm._http = httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer fake"})
    assert llm.complete_json("sys", "user") == {"ok": True}
    assert calls[0].url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert calls[0].headers["authorization"] == "Bearer fake"
    assert llm.calls == 2


def test_budget_limit(monkeypatch):
    import httpx
    from display_intel.llm import LLM

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    llm = LLM("gemini", "m", max_calls=1)
    llm._http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})))
    llm.complete_json("s", "u")
    with pytest.raises(BudgetExceeded):
        llm.complete_json("s", "u")


def test_extract_splits_batch_on_truncation():
    from display_intel.llm import OutputTruncated
    arts = [art(f"Acme Foo {i} laptop OLED", f"Acme Foo {i} has a 14-inch OLED panel.") for i in range(2)]
    ok = {"results": [{"article_id": 1, "relevant": True, "panels": [
        {"brand": "Acme", "model": "Foo", "panel_tech": "OLED", "evidence": {"panel_tech": "OLED panel"}}]}]}
    llm = FakeLLM([OutputTruncated("太長"), ok, ok])
    results, stats = extract_all(arts, llm, {"max_chars_per_call": 10000})
    assert stats["split_batches"] == 1 and stats["done_batches"] == 2
    assert len(results) == 2 and llm.calls == 3
    assert stats["errors"]


def test_working_llm_falls_back(monkeypatch):
    from display_intel import __main__ as main
    from display_intel.llm import LLM
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(LLM, "probe", lambda self: "ok" if self.model == "good" else "HTTPStatusError：404 unknown model")
    stats = {}
    llm = main._working_llm({"llm": {"provider": "gemini", "extract_model": "bad", "fallback_models": ["good"]}}, stats)
    assert llm.model == "good" and stats["llm_model"] == "good"
    assert "404" in stats["llm_probe"]["bad"]


def test_non_json_body_gives_clear_error(monkeypatch):
    import httpx
    from display_intel.llm import LLM
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    llm = LLM("gemini", "m", max_calls=3)
    llm._http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>retired</html>")))
    with pytest.raises(RuntimeError, match="回覆格式異常"):
        llm.complete_json("s", "u")


def test_github_models_retired_message():
    from display_intel.llm import LLM
    with pytest.raises(RuntimeError, match="gemini"):
        LLM("github_models", "x")


def test_unidentifiable_panels_dropped():
    from display_intel.extract import is_identifiable, postprocess
    assert is_identifiable(PanelSpec(brand="HP", model="OmniBook 5"))
    assert not is_identifiable(PanelSpec(brand=None, model="Laptop"))
    assert not is_identifiable(PanelSpec(brand="MSI", model=None))
    assert not is_identifiable(PanelSpec(brand="Asus", model="筆電"))
    a = art("MSI laptop", "MSI shows a new laptop with a 240Hz OLED display.")
    ex = Extraction(relevant=True, panels=[PanelSpec(brand="MSI", refresh_hz=240, evidence={"refresh_hz": "240Hz"})])
    ex, flags = postprocess(a, ex)
    assert ex.panels == [] and any("缺少品牌或機型" in f for f in flags)


def test_tavily_request(monkeypatch):
    import json as _json
    import httpx
    from display_intel.collect import _tavily
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake")
    seen = []

    def handler(req):
        seen.append(_json.loads(req.content))
        return httpx.Response(200, json={"results": [
            {"url": "https://n.com/a?utm_source=x", "title": "Acme laptop OLED", "content": "snippet",
             "raw_content": "Acme Foo 14 laptop has a 14-inch OLED display. " * 30}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = {"sources": {"tavily": {"queries": ["q1", "q2", "q3"], "max_queries_per_run": 2}}}
    out = _tavily(client, cfg)
    assert len(seen) == 2 and seen[0]["time_range"] == "day" and seen[0]["include_raw_content"] == "text"
    assert out[0].content_level == "full" and out[0].url == "https://n.com/a"


def test_weekly_cadence(db, tmp_path):
    from display_intel.collect import google_news_urls
    from display_intel.report import build_report
    cfg = {"schedule": {"cadence": "weekly", "timezone": "Asia/Taipei"},
           "sources": {"google_news": {"locales": [{"hl": "en-US", "gl": "US", "ceid": "US:en", "lang": "en"}],
                                       "queries": {"en": ["laptop OLED"]}}},
           "report": {"output_dir": str(tmp_path), "subject_prefix": "[週報]"}}
    assert "when%3A7d" in google_news_urls(cfg)[0]["url"]
    ctx, html, xlsx, ids = build_report(db, cfg)
    assert ctx["title"] == "筆電顯示器情報週報" and "過去 7 天" in html and "本週重點" in html


# ---------- 面板型號解碼與合併 ----------

def test_panel_decode():
    from display_intel.panel_decode import canonical_maker, decode
    assert decode("NE160QDM-NY1") == {"maker": "BOE", "size_inch": 16.0}
    assert decode("ATNA56YX03") == {"maker": "Samsung Display", "size_inch": 15.6}
    assert decode("B140HAN06.8") == {"maker": "AUO", "size_inch": 14.0}
    assert decode("N156HCA-EAC")["maker"] == "Innolux"
    assert decode("LP160UQ1-SPB1")["maker"] == "LG Display"
    assert decode("XYZ123") is None
    assert canonical_maker("京东方") == "BOE" and canonical_maker("Samsung") == "Samsung Display"
    assert canonical_maker("SomeNewMaker") == "SomeNewMaker"


def test_inferred_maker_and_merge(db):
    a = PanelSpec(brand="ASUS", model="ProArt P16（H7607）", panel_tech="OLED", size_inch=16, res_w=3840, res_h=2400)
    b = PanelSpec(brand="ASUS", model="ASUS ProArt P16", panel_tech="OLED", panel_model="ATNA60CL10", refresh_hz=120)
    assert verify.model_norm(a) == verify.model_norm(b) == "proartp16"
    ev1 = verify.merge_panel(db, a, Confidence.C, art("a"), [])
    ev2 = verify.merge_panel(db, b, Confidence.C, art("b"), [])
    assert ev1.kind == "new" and ev2.kind == "updated"
    assert len(db.all_panels()) == 1
    rec = db.all_panels()[0]
    assert rec.spec.refresh_hz == 120 and rec.panel_maker_inferred == "Samsung Display" and rec.maker == "Samsung Display"
    # 不同尺寸不能合併
    c = PanelSpec(brand="ASUS", model="ProArt P16", panel_tech="OLED", size_inch=14)
    assert verify.merge_panel(db, c, Confidence.C, art("c"), []).kind == "new"


def test_consolidate_existing_duplicates(db):
    from display_intel.db import utcnow
    from display_intel.schema import PanelRecord
    now = utcnow()
    for key, spec in [("k1", PanelSpec(brand="HP", model="OMEN 16", res_w=2560, res_h=1600)),
                      ("k2", PanelSpec(brand="HP", model="HP OMEN 16 (16-xd0000)", refresh_hz=240))]:
        db.put_panel(PanelRecord(key=key, spec=spec, confidence=Confidence.C, source_url="u", source_name="s",
                                 first_seen=now, last_updated=now))
    assert verify.consolidate(db) == 1
    rec = db.all_panels()[0]
    assert rec.spec.refresh_hz == 240 and rec.spec.res_w == 2560


def test_fix_panel_source():
    from display_intel.extract import fix_panel_source
    s, flags = fix_panel_source(PanelSpec(brand="ASUS", model="ProArt P16", panel_maker="ASUS", panel_model="Lumina Pro OLED"))
    assert s.panel_maker is None and s.panel_model is None and len(flags) == 2
    s, _ = fix_panel_source(PanelSpec(brand="HP", model="X", panel_maker="京东方", panel_model="NE160QDM-NY1"))
    assert s.panel_maker == "BOE" and s.panel_model == "NE160QDM-NY1"
    s, _ = fix_panel_source(PanelSpec(brand="HP", model="X", panel_model="TL140ADXP01"))
    assert s.panel_model == "TL140ADXP01"  # 認不得但像料號 → 保留


# ---------- 報告分析 ----------

def _rec(key, days_ago, conf=Confidence.C, **spec):
    from datetime import timedelta
    from display_intel.db import utcnow
    from display_intel.schema import PanelRecord
    t = utcnow() - timedelta(days=days_ago)
    r = PanelRecord(key=key, spec=PanelSpec(**spec), confidence=conf, source_url="u", source_name="s", first_seen=t, last_updated=t)
    return verify._derived(r)


def test_analytics_blocks():
    from datetime import timedelta
    from display_intel import analytics
    from display_intel.db import utcnow
    start = utcnow() - timedelta(days=7)
    recs = [
        _rec("a", 1, brand="ASUS", model="A", panel_tech="Tandem OLED", refresh_hz=120, res_w=2880, res_h=1800, size_inch=14, panel_maker="Samsung Display"),
        _rec("b", 2, brand="HP", model="B", panel_tech="IPS LCD", refresh_hz=240, panel_model="NE160QDM-NY1"),
        _rec("c", 10, brand="HP", model="C", panel_tech="OLED", refresh_hz=60),
        _rec("d", 3, Confidence.D, brand="Apple", model="D", panel_tech="OLED"),
    ]
    assert analytics.features(recs[0].spec) >= {"OLED", "Tandem OLED", "高解析 2.8K+"}
    rows = [{"rec": r} for r in recs[:2]]
    k = {x["label"]: x["value"] for x in analytics.kpis(rows)}
    assert k["新機型"] == "2" and k["OLED 占比"] == "50%" and k["刷新率中位數"] == "180Hz"
    lines, tags = analytics.standouts(rows, recs, start)
    assert any("240Hz" in l for l in lines) and "最高刷新率" in tags["b"]
    assert any("首見技術：Tandem OLED" in l for l in lines)  # 本期之前只有 C（一般 OLED）
    wow = {w["metric"]: w for w in analytics.week_over_week(recs, start)}
    assert wow["新機型數"]["now"] == "2" and wow["新機型數"]["prev"] == "1" and wow["新機型數"]["arrow"] == "▲"
    m = analytics.brand_maker_matrix(recs)
    assert set(m["makers"]) == {"Samsung Display", "BOE"} and m["inferred"] == 1
    assert [t["name"] for t in analytics.rumor_tracker(recs, utcnow())] == ["Apple D"]
    assert analytics.week_over_week(recs[:2], start) is None  # 沒有上期資料


# ---------- 規格補完 ----------

def test_completeness_and_candidates(db):
    from display_intel import analytics, enrich
    sparse = _rec("s", 2, brand="HP", model="OmniBook 5", panel_tech="OLED")
    rich = _rec("r", 2, brand="ASUS", model="ProArt P16", panel_tech="OLED", size_inch=16, res_w=3840, res_h=2400,
                refresh_hz=120, sdr_nits=500, dci_p3_pct=100, panel_maker="Samsung Display", panel_model="ATNA60",
                response_ms=0.2, dimming="DC", surface="glossy", delta_e=1)
    rumor = _rec("d", 2, Confidence.D, brand="Apple", model="MacBook Pro")
    old = _rec("o", 90, brand="HP", model="Old")
    assert analytics.completeness(rich) == (12, 12) and analytics.completeness(sparse)[0] == 1
    for r in (sparse, rich, rumor, old):
        db.put_panel(r)
    col = {"enrich_min_missing": 4, "enrich_within_days": 45}
    assert [r.key for r in enrich.candidates(db, col)] == ["s"]
    sparse.enrich_attempts, sparse.last_enriched = 1, sparse.first_seen
    db.put_panel(sparse)
    from display_intel.db import utcnow
    sparse.last_enriched = utcnow()
    db.put_panel(sparse)
    assert enrich.candidates(db, {**col, "enrich_retry_days": 6}) == []  # 剛補查過


def test_enrich_end_to_end(db, monkeypatch):
    import httpx
    from display_intel import enrich as enr
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake")
    rec = _rec("x", 1, brand="HP", model="OmniBook 5", panel_tech="OLED")
    db.put_panel(rec)
    page = ("HP OmniBook 5 review. The OmniBook 5 has a 14-inch 1920x1200 OLED panel from Samsung Display "
            "with 400 nits and 100% DCI-P3. The Acme X1 has a 4K screen. ") * 3

    def handler(req):
        return httpx.Response(200, json={"results": [
            {"url": "https://www.notebookcheck.net/HP-OmniBook-5-review.1.0.html", "title": "HP OmniBook 5 review",
             "content": "review", "raw_content": page},
            {"url": "https://other.com/acme", "title": "Acme X1", "content": "x", "raw_content": "Acme X1 laptop " * 100}]})

    real_client = httpx.Client
    monkeypatch.setattr(enr.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    reply = {"results": [{"article_id": 1, "relevant": True, "panels": [
        {"brand": "HP", "model": "OmniBook 5", "panel_tech": "OLED", "panel_maker": "Samsung Display",
         "size_inch": 14, "res_w": 1920, "res_h": 1200, "sdr_nits": 400, "dci_p3_pct": 100,
         "evidence": {"panel_tech": "OLED panel", "panel_maker": "Samsung Display", "size_inch": "14-inch",
                      "res_w": "1920x1200", "res_h": "1920x1200", "sdr_nits": "400 nits", "dci_p3_pct": "100% DCI-P3"}},
        {"brand": "Acme", "model": "X1", "res_w": 3840, "evidence": {}}]}]}
    cfg = {"collection": {"enrich_min_missing": 4}, "llm": {"max_chars_per_call": 50000, "max_chars_per_article": 3000,
                                                            "max_chars_per_review": 8000}}
    stats = enr.enrich(db, cfg, FakeLLM([reply]), limit=5)
    assert stats["searched"] == 1 and stats["pages"] == 1  # 沒提到 OmniBook 5 的頁面被過濾
    assert stats["improved"] == 1 and stats["fields_added"] >= 4
    recs = db.all_panels()
    assert len(recs) == 1 and recs[0].maker == "Samsung Display" and recs[0].confidence == Confidence.B
    assert recs[0].enrich_attempts == 1
    assert any(e["kind"] == "updated" for e in db.unreported_events())


def test_profiles_schedule():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from display_intel.config import apply_profile, load_config
    cfg = load_config()
    tz = ZoneInfo("Asia/Taipei")
    apply_profile(cfg, datetime(2026, 10, 14, 19, 17, tzinfo=tz))  # 週三
    assert cfg["run"]["profile"] == "light" and cfg["run"]["tavily_days"] == 2 and len(cfg["run"]["tavily_queries"]) == 10
    apply_profile(cfg, datetime(2026, 10, 18, 19, 17, tzinfo=tz))  # 週日
    assert cfg["run"]["profile"] == "full" and cfg["run"]["extract_model"] and cfg["run"]["enrich_models"] == 15
    apply_profile(cfg, profile="enrich")
    assert cfg["run"]["collect_news"] is False and cfg["run"]["tavily_queries"] == []


def test_identity_and_grouping(db, tmp_path):
    from display_intel.extract import normalize_identity
    s = normalize_identity(PanelSpec(brand="Asus", model="ASUS Zenbook 14"))
    assert (s.brand, s.model) == ("ASUS", "Zenbook 14")
    s = normalize_identity(PanelSpec(brand="Framework", model="Framework Laptop 12"))
    assert s.model == "Laptop 12"
    assert normalize_identity(PanelSpec(brand="dell alienware", model="x")).brand == "Dell alienware"
    from display_intel.report import build_report
    a = art("OMEN", "HP OMEN 15 has a 15.6-inch 1920x1080 panel and a 2560x1440 panel option.", source_type="official")
    ex = Extraction(relevant=True, panels=[
        PanelSpec(brand="HP", model="OMEN 15", size_inch=15.6, res_w=1920, res_h=1080),
        PanelSpec(brand="HP", model="OMEN 15", size_inch=15.6, res_w=2560, res_h=1440)])
    verify.ingest(db, [(a, ex, [])])
    assert len(db.all_panels()) == 2  # 兩種選配分開記錄
    cfg = {"schedule": {"timezone": "Asia/Taipei"}, "report": {"output_dir": str(tmp_path)}}
    ctx, html, _, _ = build_report(db, cfg)
    assert len(ctx["new_panels"]) == 1 and len(ctx["new_panels"][0]["options"]) == 2
    assert "2 種螢幕選配" in html and ctx["kpis"][0]["value"] == "1"
