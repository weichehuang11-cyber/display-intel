"""報告層：把未報告的事件整理成 HTML Email 與 Excel 附件。"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from . import analytics
from .db import DB
from .panel_decode import canonical_maker
from .schema import Confidence, PanelRecord, PanelSpec

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"

FIELD_LABELS = {
    "brand": "品牌", "model": "機型", "panel_maker": "面板廠", "panel_model": "面板型號", "panel_tech": "面板技術",
    "size_inch": "尺寸", "res_w": "水平解析度", "res_h": "垂直解析度", "surface": "表面處理",
    "sdr_nits": "SDR 亮度", "hdr_peak_nits": "HDR 峰值亮度", "contrast": "對比", "srgb_pct": "sRGB",
    "dci_p3_pct": "DCI-P3", "adobe_rgb_pct": "Adobe RGB", "color_depth_bit": "色深", "delta_e": "ΔE",
    "hdr_formats": "HDR 格式", "certifications": "認證", "refresh_hz": "刷新率", "vrr_range": "VRR 範圍",
    "response_ms": "反應時間", "vrr_tech": "VRR 技術", "backlight": "背光", "mini_led_zones": "Mini LED 分區",
    "dimming": "調光", "pwm_hz": "PWM 頻率", "low_power_tech": "節能技術", "eye_care": "護眼",
    "touch": "觸控", "stylus": "觸控筆", "glass": "玻璃", "screen_to_body_pct": "屏佔比", "form_factor": "形態",
}
VENDOR_ORDER = ["Intel", "NVIDIA", "AMD", "Qualcomm"]
FAB_STATUS = {"planned": "規劃", "construction": "建廠", "equipment": "設備進駐", "trial": "試產",
              "mass_production": "量產", "shutdown": "停產", "other": "動態"}
PLATFORM_CATEGORY = {"sync": "同步技術", "power": "節能技術", "image": "畫質技術", "interface": "介面標準",
                     "certification": "平台認證", "other": "其他"}


def _fmt(v) -> str:
    if v is None or v == []:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return "、".join(map(str, v))
    return str(v)


def _current_record(db: DB, ev: dict) -> PanelRecord:
    """事件裡存的是當時的快照；報告改用資料庫中最新、已合併的紀錄。"""
    snap = ev["payload"]["record"]
    rec = db.get_panel(snap["key"])
    if rec is None:
        from .verify import find_existing
        rec = find_existing(db, PanelSpec(**snap["spec"]))
    return rec or PanelRecord.model_validate(snap)


def panel_card(rec: PanelRecord, changes: dict | None = None, tags: list[str] | None = None) -> dict:
    s = rec.spec
    if s.size_inch:
        size = f'{s.size_inch:g}"'
    elif rec.size_inferred:
        size = f'{rec.size_inferred:g}"（推算）'
    else:
        size = ""
    screen = "・".join(x for x in [size, s.panel_tech or "", f"{s.res_w}×{s.res_h}" if s.res_w and s.res_h else "",
                                   f"{s.refresh_hz:g}Hz" if s.refresh_hz else ""] if x)
    bright = "・".join(x for x in [f"SDR {s.sdr_nits:g} nits" if s.sdr_nits else "",
                                   f"HDR {s.hdr_peak_nits:g} nits" if s.hdr_peak_nits else "",
                                   f"DCI-P3 {s.dci_p3_pct:g}%" if s.dci_p3_pct else "",
                                   f"sRGB {s.srgb_pct:g}%" if s.srgb_pct else "",
                                   f"ΔE {s.delta_e:g}" if s.delta_e else ""] if x)
    maker = rec.maker
    maker_txt = (f"{maker}（由型號推算）" if rec.panel_maker_inferred else maker) if maker else ""
    panel = " ".join(x for x in [maker_txt, s.panel_model or ""] if x)
    extras = "・".join(x for x in [
        _fmt(s.vrr_tech), f"Mini LED {s.mini_led_zones} 區" if s.mini_led_zones else "",
        f"{s.dimming} 調光" if s.dimming else "", f"PWM {s.pwm_hz:g}Hz" if s.pwm_hz else "",
        _fmt(s.low_power_tech), _fmt(s.certifications), _fmt(s.hdr_formats),
        "觸控" if s.touch else "", s.surface or ""] if x)
    derived = "・".join(x for x in [f"{rec.ppi:g} PPI" if rec.ppi else "", rec.aspect_ratio or "",
                                    f"像素密度{rec.density_class}" if rec.density_class else "",
                                    ("面板原廠規格：" + "、".join(FIELD_LABELS.get(f, f) for f in rec.panel_spec_fields))
                                    if rec.panel_spec_fields else ""] if x)
    change_text = "；".join(f"{FIELD_LABELS.get(k, k)}：{_fmt(o) or '—'} → {_fmt(n)}" for k, (o, n) in (changes or {}).items())
    known, total = analytics.completeness(rec)
    return {
        "score": f"{known}/{total}", "score_pct": round(known / total * 100), "missing": "、".join(analytics.missing_fields(rec)),
        "key": rec.key, "rec": rec, "brand": s.brand or "", "model": s.model or "",
        "screen": screen, "bright": bright, "panel": panel or "面板來源待確認", "panel_known": bool(panel),
        "extras": extras, "derived": derived, "tags": tags or [],
        "confidence": rec.confidence.label, "conf_code": rec.confidence.value,
        "source_name": rec.source_name, "source_url": rec.source_url, "changes": change_text,
        "spec": s.model_dump(), "record": rec.model_dump(mode="json"),
        # 舊欄位（AI 摘要與 Excel 使用）
        "size": size, "tech": s.panel_tech or "", "res": f"{s.res_w}×{s.res_h}" if s.res_w and s.res_h else "",
        "refresh": f"{s.refresh_hz:g}Hz" if s.refresh_hz else "", "panel_src": panel or "待確認",
    }


def group_by_model(cards: list[dict]) -> list[dict]:
    """同一款機型的不同螢幕選配合併成一張卡片（例如 OMEN 15 的 FHD 144Hz 與 QHD 165Hz）。"""
    from .verify import _norm, model_norm
    groups: dict[tuple, list[dict]] = {}
    for c in cards:
        k = (_norm(c["rec"].spec.brand), model_norm(c["rec"].spec))
        groups.setdefault(k, []).append(c)
    out = []
    for opts in groups.values():
        opts.sort(key=lambda c: (-c["rec"].confidence.rank, -c["score_pct"]))
        best = max(opts, key=lambda c: c["score_pct"])
        head = dict(opts[0])
        head["options"] = opts
        head["tags"] = list(dict.fromkeys(t for c in opts for t in c["tags"]))
        head["score"], head["score_pct"] = best["score"], best["score_pct"]
        head["panel_known"] = any(c["panel_known"] for c in opts)
        head["sources"] = list({c["source_url"]: c for c in opts if c["source_url"]}.values())[:3]
        out.append(head)
    return sorted(out, key=lambda c: (c["brand"].lower(), c["model"].lower()))


def ctx_label(weekly: bool) -> str:
    return "本週" if weekly else "今日"


def build_context(events: list[dict], db: DB, cfg: dict, today: datetime) -> dict:
    weekly = (cfg.get("schedule") or {}).get("cadence", "daily") == "weekly"
    period_start = (today - timedelta(days=7 if weekly else 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # 同一款機型在本期可能有多個事件（新增後又補規格），合併成一張卡片
    priority = {"rumor_confirmed": 3, "new": 2, "updated": 1}
    grouped: dict[str, dict] = {}
    for e in (e for e in events if e["category"] == "panel"):
        rec = _current_record(db, e)
        g = grouped.setdefault(rec.key, {"rec": rec, "kind": e["kind"], "changes": {}})
        if priority.get(e["kind"], 0) > priority.get(g["kind"], 0):
            g["kind"] = e["kind"]
        g["changes"].update(e["payload"].get("changes") or {})
    all_records = db.all_panels()
    period_new = [{"rec": g["rec"]} for g in grouped.values() if g["kind"] == "new" and g["rec"].confidence != Confidence.D]
    standout_lines, tags = analytics.standouts(period_new, all_records, period_start)

    def card(g):
        return panel_card(g["rec"], g["changes"] if g["kind"] != "new" else None, tags.get(g["rec"].key))

    by_name = lambda c: (c["brand"].lower(), c["model"].lower())
    is_rumor = lambda g: g["rec"].confidence == Confidence.D
    new_panels = sorted((card(g) for g in grouped.values() if g["kind"] == "new" and not is_rumor(g)), key=by_name)
    updates = sorted((card(g) for g in grouped.values() if g["kind"] == "updated" and not is_rumor(g)), key=by_name)
    confirmed = sorted((card(g) for g in grouped.values() if g["kind"] == "rumor_confirmed"), key=by_name)
    rumor_panels = sorted((card(g) for g in grouped.values() if is_rumor(g)), key=by_name)
    ev_rumor = lambda e: e.get("confidence") == "D"

    platform = [e for e in events if e["category"] == "platform"]
    platform_rows = sorted(
        [{"vendor": e["payload"]["vendor"], "tech": e["payload"]["tech_name"],
          "category": PLATFORM_CATEGORY.get(e["payload"]["category"], ""),
          "summary": e["payload"]["summary"], "affected": "、".join(FIELD_LABELS.get(x, x) for x in e["payload"].get("affected_specs", [])),
          "confidence": Confidence(e["confidence"]).label if e.get("confidence") else "",
          "conf_code": e.get("confidence") or "C",
          "source_name": e["source_name"], "source_url": e["source_url"], "rumor": ev_rumor(e) or e["kind"] == "rumor"}
         for e in platform],
        key=lambda r: VENDOR_ORDER.index(r["vendor"]) if r["vendor"] in VENDOR_ORDER else 99)
    fab_rows = [{"maker": canonical_maker(e["payload"]["maker"]), "site": e["payload"].get("fab_site") or "",
                 "gen": e["payload"].get("generation") or "", "tech": e["payload"].get("technology") or "",
                 "status": FAB_STATUS.get(e["kind"], e["kind"]), "milestone": e["payload"].get("milestone") or "",
                 "capacity": e["payload"].get("capacity_sheets_per_month"),
                 "summary": e["payload"]["summary"], "confidence": Confidence(e["confidence"]).label if e.get("confidence") else "",
                 "source_name": e["source_name"], "source_url": e["source_url"], "rumor": ev_rumor(e)}
                for e in events if e["category"] == "fab"]
    industry = [{"note": e["payload"]["note"], "source_name": e["source_name"], "source_url": e["source_url"]}
                for e in events if e["category"] == "industry"]

    ctx = {
        "date": today.strftime("%Y-%m-%d"), "weekday": "一二三四五六日"[today.weekday()],
        "new_panels": new_panels, "updates": updates, "confirmed": confirmed,
        "rumor_panels": rumor_panels,
        "platform": [r for r in platform_rows if not r["rumor"]],
        "platform_rumors": [r for r in platform_rows if r["rumor"]],
        "fabs": [r for r in fab_rows if not r["rumor"]], "fab_rumors": [r for r in fab_rows if r["rumor"]],
        "industry": industry,
        "kpis": analytics.kpis(new_panels),
        "new_options": len(new_panels),
        "standouts": standout_lines,
        "wow": analytics.week_over_week(all_records, period_start, 7 if weekly else 1),
        "mentions": analytics.tech_mentions(all_records, period_start),
        "matrix": analytics.brand_maker_matrix(all_records),
        "rumor_tracker": analytics.rumor_tracker(all_records, today),
        "to_fill": analytics.to_fill(all_records, today),
    }
    # 卡片依機型合併（螢幕選配列在同一張卡片內）
    for k in ("new_panels", "updates", "confirmed", "rumor_panels"):
        ctx[k + "_flat"] = ctx[k]
        ctx[k] = group_by_model(ctx[k])
    ctx["panel_known"] = sum(1 for c in ctx["new_panels"] if c["panel_known"])
    if ctx["kpis"]:
        ctx["kpis"][0] = {"label": "新機型", "value": str(len(ctx["new_panels"])),
                          "note": f"共 {len(new_panels)} 種螢幕選配"}
        if ctx["new_panels"]:  # 完整度以每款機型資料最完整的選配計算，與卡片一致
            avg = sum(c["score_pct"] for c in ctx["new_panels"]) / len(ctx["new_panels"])
            for k in ctx["kpis"]:
                if k["label"] == "平均規格完整度":
                    k.update({"label": f"{ctx_label(weekly)}新機完整度", "value": f"{avg:.0f}%", "note": "剛發表，評測多半還沒出"})
        mature, n_mature = analytics.mature_completeness(all_records, today)
        ctx["kpis"].append({"label": "上市滿 30 天完整度", "value": f"{mature:.0%}" if mature is not None else "—",
                            "note": f"{n_mature} 款｜目標 75%" if mature is not None else "資料累積中｜目標 75%"})
    ctx["ongoing"] = ongoing(db, cfg, ctx)
    ctx["weekly"] = weekly
    ctx["title"] = "筆電顯示器情報週報" if weekly else "筆電顯示器情報日報"
    ctx["period_label"] = "本週" if weekly else "今日"
    ctx["prev_label"] = "上週" if weekly else "昨日"
    if weekly:
        ctx["period"] = f"資料期間：{(today - timedelta(days=6)).strftime('%m/%d')}–{today.strftime('%m/%d')}（過去 7 天）"
    else:
        ctx["period"] = "資料期間：前一日報告後至今"
    ctx["trend"] = weekly_trend(db) if (weekly or today.weekday() == 0) else None
    n_rumor = len(rumor_panels) + len(ctx["platform_rumors"]) + len(ctx["fab_rumors"])
    ctx["subject"] = (f"{cfg['report'].get('subject_prefix', '[顯示器情報]')} {ctx['date']}｜"
                      f"新機 {len(ctx['new_panels'])}・更新 {len(ctx['updates'])}・平台 {len(ctx['platform'])}・"
                      f"產線 {len(ctx['fabs'])}・傳聞 {n_rumor}")
    return ctx


def ongoing(db: DB, cfg: dict, ctx: dict) -> list[dict]:
    """今天沒有新消息的類別，列出近期仍在討論的主題。"""
    days = cfg["report"].get("ongoing_days", 30)
    recent = [e for e in db.recent_events(days) if e.get("reported_at")]
    out = []
    checks = [("新機型與面板", "panel", not ctx["new_panels"]),
              ("平台技術", "platform", not ctx["platform"]),
              ("面板廠產線", "fab", not ctx["fabs"])]
    for label, cat, empty in checks:
        if not empty:
            continue
        items = []
        for e in [e for e in recent if e["category"] == cat][:4]:
            p = e["payload"]
            if cat == "panel":
                s = p["record"]["spec"]
                title = f"{s.get('brand') or ''} {s.get('model') or ''} {s.get('panel_tech') or ''}".strip()
            elif cat == "platform":
                title = f"{p['vendor']} {p['tech_name']}：{p['summary']}"
            else:
                title = f"{p['maker']} {p.get('generation') or ''} {p.get('technology') or ''}：{p['summary']}"
            items.append({"title": title, "date": e["created_at"][:10], "source_url": e["source_url"]})
        out.append({"label": label, "entries": items})
    return out


def weekly_trend(db: DB) -> dict | None:
    """每週一：近 30 天新增紀錄的面板技術、刷新率分布。"""
    from datetime import timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(days=30)
    recs = [r for r in db.all_panels() if r.first_seen >= since and r.confidence.rank >= Confidence.C.rank]
    if len(recs) < 5:
        return None
    tech = Counter("OLED" if r.spec.panel_tech and "oled" in r.spec.panel_tech.lower() else
                   ("LCD" if r.spec.panel_tech else "未知") for r in recs)
    refresh = Counter(f"{r.spec.refresh_hz:g}Hz" for r in recs if r.spec.refresh_hz)
    makers = Counter(r.maker for r in recs if r.maker)
    return {"count": len(recs), "tech": tech.most_common(), "refresh": refresh.most_common(5),
            "makers": makers.most_common(5)}


def highlights(ctx: dict, llm=None) -> list[str]:
    """重點摘要：有 AI 就請 AI 寫，失敗就用規則產生。"""
    facts = {
        "新機": [f"{r['brand']} {r['model']} {r['size']} {r['tech']} {r['res']} {r['refresh']} {r['panel_src']}" for r in ctx["new_panels"][:15]],
        "規格更新": [f"{r['brand']} {r['model']}：{r['changes']}" for r in ctx["updates"][:10]],
        "傳聞證實": [f"{r['brand']} {r['model']}" for r in ctx["confirmed"]],
        "平台": [f"{r['vendor']} {r['tech']}：{r['summary']}" for r in ctx["platform"][:10]],
        "產線": [f"{r['maker']} {r['gen']} {r['status']}：{r['summary']}" for r in ctx["fabs"][:10]],
        "傳聞": [f"{r['brand']} {r['model']} {r['tech']} {r['refresh']}" for r in ctx["rumor_panels"][:10]],
        "關鍵數字": [f"{k['label']}：{k['value']}（{k['note']}）" for k in ctx.get("kpis", [])],
        "亮點": ctx.get("standouts", []),
        "與上期比較": [f"{w['metric']}：{w['prev']} → {w['now']}" for w in (ctx.get("wow") or [])],
    }
    if llm is not None and any(facts.values()):
        try:
            data = llm.complete_json(
                f"你是筆電顯示器市場分析師。根據提供的{ctx.get('period_label', '今日')}資料，用繁體中文寫 3 到 5 句重點，最重要的放第一句。"
                "只能使用提供的資料，不可加入資料以外的資訊。傳聞必須註明是傳聞。"
                "最後一句寫一個值得關注的市場或技術觀察（例如哪類規格變多、哪家面板廠出現較多），但仍只能根據資料。"
                '只輸出 JSON：{"highlights": ["...", "..."]}',
                json.dumps(facts, ensure_ascii=False))
            hl = [h for h in data.get("highlights", []) if isinstance(h, str) and h.strip()]
            if hl:
                return hl[:5]
        except Exception as exc:
            log.warning("AI 重點摘要失敗，改用規則摘要：%s", exc)
    out = []
    if ctx["new_panels"]:
        techs = Counter(r["tech"] or "未標示" for r in ctx["new_panels"])
        out.append(f"新增 {len(ctx['new_panels'])} 款機型面板紀錄，面板技術分布："
                   + "、".join(f"{k} {v} 款" for k, v in techs.most_common()) + "。")
    if ctx["updates"]:
        out.append(f"{len(ctx['updates'])} 款既有機型補上或修正規格。")
    if ctx["confirmed"]:
        out.append(f"{len(ctx['confirmed'])} 則先前傳聞獲得證實。")
    if ctx["platform"]:
        out.append(f"平台廠動態 {len(ctx['platform'])} 則：" + "、".join(sorted({r['vendor'] for r in ctx['platform']})) + "。")
    if ctx["fabs"]:
        out.append(f"面板廠產線動態 {len(ctx['fabs'])} 則。")
    return out or [f"{ctx.get('period_label', '今日')}沒有新的官方或媒體消息，下方列出近期仍在討論的主題。"]


def render_html(ctx: dict) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    return env.get_template("report.html.j2").render(**ctx)


def write_excel(ctx: dict, db: DB, path: Path) -> Path:
    wb = Workbook()
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E79")
    fields = [f for f in FIELD_LABELS]

    def sheet(ws, headers, rows):
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(40, max(10, max(len(str(c.value or "")) for c in col) + 2))

    ws = wb.active
    ws.title = f"{ctx.get('period_label', '今日')}新增與更新"
    rows = []
    for kind, items in [("新機", ctx.get("new_panels_flat", ctx["new_panels"])), ("更新", ctx.get("updates_flat", ctx["updates"])),
                        ("傳聞證實", ctx.get("confirmed_flat", ctx["confirmed"])), ("傳聞", ctx.get("rumor_panels_flat", ctx["rumor_panels"]))]:
        for r in items:
            s, rec = r["spec"], r["record"]
            rows.append([kind, r["confidence"]] + [_fmt(s.get(f)) for f in fields]
                        + [rec.get("ppi"), rec.get("aspect_ratio"), rec.get("density_class"), r["changes"], r["source_name"], r["source_url"]])
    sheet(ws, ["類型", "可信度"] + [FIELD_LABELS[f] for f in fields] + ["PPI", "長寬比", "像素密度", "變更", "來源", "網址"], rows)
    ws.auto_filter.ref = ws.dimensions

    ws2 = wb.create_sheet("全部面板紀錄")
    sheet(ws2, ["可信度"] + [FIELD_LABELS[f] for f in fields]
          + ["面板廠（含推算）", "PPI", "長寬比", "像素密度", "首次出現", "最後更新", "來源", "網址", "查核標記"],
          [[rec.confidence.label] + [_fmt(getattr(rec.spec, f)) for f in fields]
           + [(f"{rec.maker}（推算）" if rec.panel_maker_inferred else rec.maker) or "",
              rec.ppi, rec.aspect_ratio, rec.density_class, rec.first_seen.strftime("%Y-%m-%d"),
              rec.last_updated.strftime("%Y-%m-%d"), rec.source_name, rec.source_url, "；".join(rec.flags)]
           for rec in db.all_panels()])
    ws2.auto_filter.ref = ws2.dimensions

    m = ctx.get("matrix")
    if m:
        ws4 = wb.create_sheet("品牌×面板廠")
        sheet(ws4, ["品牌"] + m["makers"], [[r["brand"]] + r["cells"] for r in m["rows"]])
    if ctx.get("mentions"):
        ws5 = wb.create_sheet("技術提及率")
        sheet(ws5, ["技術", "說明", f"{ctx.get('period_label', '本期')}款數", f"{ctx.get('period_label', '本期')}提及率", "前 30 天提及率"],
              [[r["feature"], r["desc"], r["count"], r["share"], r["base_share"]] for r in ctx["mentions"]])

    ws3 = wb.create_sheet("平台與產線")
    sheet(ws3, ["類別", "廠商", "項目", "分類／狀態", "摘要", "可信度", "來源", "網址"],
          [["平台", r["vendor"], r["tech"], r["category"], r["summary"], r["confidence"], r["source_name"], r["source_url"]]
           for r in ctx["platform"] + ctx["platform_rumors"]]
          + [["產線", r["maker"], f"{r['site']} {r['gen']} {r['tech']}".strip(), r["status"], r["summary"], r["confidence"], r["source_name"], r["source_url"]]
             for r in ctx["fabs"] + ctx["fab_rumors"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def build_report(db: DB, cfg: dict, llm=None) -> tuple[dict, str, Path, list[int]]:
    """產生報告。回傳（內容, HTML, Excel 路徑, 已報告的事件 id）。"""
    tz = ZoneInfo(cfg["schedule"].get("timezone", "Asia/Taipei"))
    today = datetime.now(tz)
    events = db.unreported_events()
    ctx = build_context(events, db, cfg, today)
    ctx["highlights"] = highlights(ctx, llm)
    ctx["glossary"] = []
    html = render_html(ctx)
    ctx["glossary"] = analytics.glossary_for(html)
    html = render_html(ctx)
    out = Path(cfg["report"]["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / f"report-{ctx['date']}.html").write_text(html, encoding="utf-8")
    xlsx = write_excel(ctx, db, out / f"display-intel-{ctx['date']}.xlsx")
    return ctx, html, xlsx, [e["id"] for e in events]
