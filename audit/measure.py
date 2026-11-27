"""每週稽核用的量測（只讀資料庫，不修改）。用法：PYTHONPATH=src python audit/measure.py [data/intel.sqlite]
輸出 JSON，欄位說明見 AUDIT.md「量測指標」。"""
import collections, json, re, sqlite3, sys
from datetime import datetime, timedelta, timezone

from display_intel import analytics, derive
from display_intel.extract import BRAND_CANON
from display_intel.schema import PanelRecord

path = sys.argv[1] if len(sys.argv) > 1 else "data/intel.sqlite"
c = sqlite3.connect(path)
recs = [PanelRecord.model_validate_json(d) for (d,) in c.execute("select data from panels")]
live = [r for r in recs if r.confidence.value != "E"]
now = datetime.now(timezone.utc)
out = {"at": now.isoformat(timespec="minutes"), "records": len(recs), "by_conf": dict(collections.Counter(r.confidence.value for r in recs))}

# 1. 收錄範圍：AIO 裡疑似外接螢幕、21 吋以上非 AIO
aio = [r for r in live if (r.spec.product_type or "").lower() == "aio"]
out["aio"] = len(aio)
out["aio_suspect_monitor"] = [f"{r.spec.brand} {r.spec.model}" for r in aio if derive.non_laptop_reason(r.spec, r.source_url)]
# 2. 完整度：上市滿 30 天的機型（目標 75%）
old = [r for r in live if r.first_seen and r.first_seen < now - timedelta(days=30)]
if old:
    got = [analytics.completeness(r) for r in old]
    out["completeness_30d_pct"] = round(100 * sum(a for a, b in got) / sum(b for a, b in got), 1)
out["panel_maker_known_pct"] = round(100 * sum(bool(r.spec.panel_maker) for r in live) / max(1, len(live)), 1)
out["panel_model_known_pct"] = round(100 * sum(bool(r.spec.panel_model) for r in live) / max(1, len(live)), 1)
# 3. 正規化：表面處理認不得、品牌不在標準清單、面板技術怪值
out["surface_unmapped"] = dict(collections.Counter(r.spec.surface for r in live if r.spec.surface and not derive.surface_code(r.spec.surface)).most_common(10))
out["brand_noncanon"] = dict(collections.Counter(r.spec.brand for r in live if r.spec.brand and r.spec.brand.split()[0].lower() not in BRAND_CANON).most_common(15))
out["panel_tech_values"] = dict(collections.Counter(str(r.spec.panel_tech) for r in live))
# 4. 數值範圍（極端值給人看）
rng = {}
for f in ["sdr_nits", "hdr_peak_nits", "refresh_hz", "response_ms", "dci_p3_pct", "size_inch", "pwm_hz"]:
    v = sorted(x for x in (getattr(r.spec, f) for r in live) if isinstance(x, (int, float)))
    if v:
        rng[f] = [len(v), v[0], v[len(v) // 2], v[-1]]
out["ranges_n_min_med_max"] = rng
# 5. 旗標：防幻覺攔截量、來源不一致（不同網站 vs 同網站）
fl = collections.Counter()
same = cross = 0
for r in live:
    for f in r.flags:
        fl[re.split("[:：（(]", f)[0]] += 1
        m = re.search(r"，(\S+) vs (\S+)）", f)
        if "來源數值不一致" in f and m:
            same += m.group(1) == m.group(2)
            cross += m.group(1) != m.group(2)
out["flags_top"] = dict(fl.most_common(12))
out["inconsistent_same_site"], out["inconsistent_cross_site"] = same, cross
# 6. 運作：近 7 天蒐集紀錄
runs = [(s, st, json.loads(x or "{}")) for s, st, x in c.execute("select stage, started, stats from runs order by id desc limit 40")]
wk = [x for x in runs if x[1] >= (now - timedelta(days=7)).isoformat()]
out["runs_7d"] = {"total": len(wk), "skipped": sum(1 for x in wk if x[2].get("skipped")),
                  "profiles": dict(collections.Counter(x[2].get("profile") for x in wk if x[2].get("profile"))),
                  "llm_models": sorted({x[2].get("llm_model") for x in wk if x[2].get("llm_model")})}
out["tavily_usage"] = {f"{m} {s}": n for m, s, n in c.execute("select month, service, count from usage")}
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
