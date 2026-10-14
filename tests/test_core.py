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
