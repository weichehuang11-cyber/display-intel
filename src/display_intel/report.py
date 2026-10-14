"""報告層：把未報告的事件整理成 HTML Email 與 Excel 附件。"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .db import DB
from .schema import Confidence

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


def panel_row(ev: dict) -> dict:
    rec = ev["payload"]["record"]
    s = rec["spec"]
    res = f"{s['res_w']}×{s['res_h']}" if s.get("res_w") and s.get("res_h") else ""
    brightness = " / ".join(x for x in [
        f"{_fmt(s.get('sdr_nits'))} nits" if s.get("sdr_nits") else "",
        f"HDR {_fmt(s.get('hdr_peak_nits'))}" if s.get("hdr_peak_nits") else ""] if x)
    color = "、".join(x for x in [
        f"DCI-P3 {_fmt(s.get('dci_p3_pct'))}%" if s.get("dci_p3_pct") else "",
        f"sRGB {_fmt(s.get('srgb_pct'))}%" if s.get("srgb_pct") else ""] if x)
    panel_src = " ".join(x for x in [s.get("panel_maker") or "", s.get("panel_model") or ""] if x)
    extras = "、".join(x for x in [
        _fmt(s.get("vrr_tech")), f"{_fmt(s.get('mini_led_zones'))} 區" if s.get("mini_led_zones") else "",
        _fmt(s.get("dimming")), f"PWM {_fmt(s.get('pwm_hz'))}Hz" if s.get("pwm_hz") else "",
        _fmt(s.get("low_power_tech")), _fmt(s.get("certifications"))] if x)
    changes = ev["payload"].get("changes") or {}
    change_text = "；".join(f"{FIELD_LABELS.get(k, k)}：{_fmt(o) or '—'} → {_fmt(n)}" for k, (o, n) in changes.items())
    return {
        "brand": s.get("brand") or "", "model": s.get("model") or "",
        "size": f"{_fmt(s.get('size_inch'))}\"" if s.get("size_inch") else "",
        "tech": s.get("panel_tech") or "", "res": res,
        "refresh": f"{_fmt(s.get('refresh_hz'))}Hz" if s.get("refresh_hz") else "",
        "brightness": brightness, "color": color, "panel_src": panel_src or "待確認",
        "ppi": f"{rec['ppi']:g}" if rec.get("ppi") else "", "density": rec.get("density_class") or "",
        "aspect": rec.get("aspect_ratio") or "", "extras": extras,
        "confidence": Confidence(ev["confidence"]).label if ev.get("confidence") else "",
        "conf_code": ev.get("confidence") or "",
        "source_name": ev.get("source_name") or "", "source_url": ev.get("source_url") or "",
        "changes": change_text, "spec": s, "record": rec,
    }


def build_context(events: list[dict], db: DB, cfg: dict, today: datetime) -> dict:
    panels = [e for e in events if e["category"] == "panel"]
    is_rumor = lambda e: e.get("confidence") == "D"
    new_panels = [panel_row(e) for e in panels if e["kind"] == "new" and not is_rumor(e)]
    updates = [panel_row(e) for e in panels if e["kind"] == "updated" and not is_rumor(e)]
    confirmed = [panel_row(e) for e in panels if e["kind"] == "rumor_confirmed"]
    rumor_panels = [panel_row(e) for e in panels if is_rumor(e)]

    platform = [e for e in events if e["category"] == "platform"]
    platform_rows = sorted(
        [{"vendor": e["payload"]["vendor"], "tech": e["payload"]["tech_name"],
          "category": PLATFORM_CATEGORY.get(e["payload"]["category"], ""),
          "summary": e["payload"]["summary"], "affected": "、".join(FIELD_LABELS.get(x, x) for x in e["payload"].get("affected_specs", [])),
          "confidence": Confidence(e["confidence"]).label if e.get("confidence") else "",
          "source_name": e["source_name"], "source_url": e["source_url"], "rumor": is_rumor(e) or e["kind"] == "rumor"}
         for e in platform],
        key=lambda r: VENDOR_ORDER.index(r["vendor"]) if r["vendor"] in VENDOR_ORDER else 99)
    fab_rows = [{"maker": e["payload"]["maker"], "site": e["payload"].get("fab_site") or "",
                 "gen": e["payload"].get("generation") or "", "tech": e["payload"].get("technology") or "",
                 "status": FAB_STATUS.get(e["kind"], e["kind"]), "milestone": e["payload"].get("milestone") or "",
                 "capacity": e["payload"].get("capacity_sheets_per_month"),
                 "summary": e["payload"]["summary"], "confidence": Confidence(e["confidence"]).label if e.get("confidence") else "",
                 "source_name": e["source_name"], "source_url": e["source_url"], "rumor": is_rumor(e)}
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
    }
    ctx["ongoing"] = ongoing(db, cfg, ctx)
    ctx["trend"] = weekly_trend(db) if today.weekday() == 0 else None
    n_rumor = len(rumor_panels) + len(ctx["platform_rumors"]) + len(ctx["fab_rumors"])
    ctx["subject"] = (f"{cfg['report'].get('subject_prefix', '[顯示器情報]')} {ctx['date']}｜"
                      f"新機 {len(new_panels)}・更新 {len(updates)}・平台 {len(ctx['platform'])}・"
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
    makers = Counter(r.spec.panel_maker for r in recs if r.spec.panel_maker)
    return {"count": len(recs), "tech": tech.most_common(), "refresh": refresh.most_common(5),
            "makers": makers.most_common(5)}


def highlights(ctx: dict, llm=None) -> list[str]:
    """今日重點：有 AI 就請 AI 寫，失敗就用規則產生。"""
    facts = {
        "新機": [f"{r['brand']} {r['model']} {r['size']} {r['tech']} {r['res']} {r['refresh']} {r['panel_src']}" for r in ctx["new_panels"][:15]],
        "規格更新": [f"{r['brand']} {r['model']}：{r['changes']}" for r in ctx["updates"][:10]],
        "傳聞證實": [f"{r['brand']} {r['model']}" for r in ctx["confirmed"]],
        "平台": [f"{r['vendor']} {r['tech']}：{r['summary']}" for r in ctx["platform"][:10]],
        "產線": [f"{r['maker']} {r['gen']} {r['status']}：{r['summary']}" for r in ctx["fabs"][:10]],
        "傳聞": [f"{r['brand']} {r['model']} {r['tech']} {r['refresh']}" for r in ctx["rumor_panels"][:10]],
    }
    if llm is not None and any(facts.values()):
        try:
            data = llm.complete_json(
                "你是筆電顯示器市場分析師。根據提供的今日資料，用繁體中文寫 3 到 5 句重點，最重要的放第一句。"
                "只能使用提供的資料，不可加入資料以外的資訊。傳聞必須註明是傳聞。"
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
    return out or ["今日沒有新的官方或媒體消息，下方列出近期仍在討論的主題。"]


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
    ws.title = "今日新增與更新"
    rows = []
    for kind, items in [("新機", ctx["new_panels"]), ("更新", ctx["updates"]), ("傳聞證實", ctx["confirmed"]), ("傳聞", ctx["rumor_panels"])]:
        for r in items:
            s, rec = r["spec"], r["record"]
            rows.append([kind, r["confidence"]] + [_fmt(s.get(f)) for f in fields]
                        + [rec.get("ppi"), rec.get("aspect_ratio"), rec.get("density_class"), r["changes"], r["source_name"], r["source_url"]])
    sheet(ws, ["類型", "可信度"] + [FIELD_LABELS[f] for f in fields] + ["PPI", "長寬比", "像素密度", "變更", "來源", "網址"], rows)

    ws2 = wb.create_sheet("全部面板紀錄")
    sheet(ws2, ["可信度"] + [FIELD_LABELS[f] for f in fields] + ["PPI", "長寬比", "像素密度", "首次出現", "最後更新", "來源", "網址", "查核標記"],
          [[rec.confidence.label] + [_fmt(getattr(rec.spec, f)) for f in fields]
           + [rec.ppi, rec.aspect_ratio, rec.density_class, rec.first_seen.strftime("%Y-%m-%d"),
              rec.last_updated.strftime("%Y-%m-%d"), rec.source_name, rec.source_url, "；".join(rec.flags)]
           for rec in db.all_panels()])

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
    html = render_html(ctx)
    out = Path(cfg["report"]["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / f"report-{ctx['date']}.html").write_text(html, encoding="utf-8")
    xlsx = write_excel(ctx, db, out / f"display-intel-{ctx['date']}.xlsx")
    return ctx, html, xlsx, [e["id"] for e in events]
