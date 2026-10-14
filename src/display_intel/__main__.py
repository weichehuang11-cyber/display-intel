"""總控（Orchestrator）：命令列入口。

用法：
  python -m display_intel collect          # 蒐集 → 萃取 → 查核 → 寫入資料庫
  python -m display_intel report           # 產生報告，等到 09:30 寄出
  python -m display_intel run              # collect + report（NAS 或本機一次跑完）
  python -m display_intel check-sources    # 測試每個來源能否連線
  python -m display_intel test-email       # 寄一封測試信
  python -m display_intel demo             # 用範例資料產生報告（不需任何金鑰）

共用選項：--dry-run 不寄信、只產生檔案；--no-wait 不等到 09:30，立刻寄出
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_config

log = logging.getLogger("display_intel")


def _at_today(cfg: dict, hhmm: str) -> datetime:
    tz = ZoneInfo(cfg["schedule"].get("timezone", "Asia/Taipei"))
    h, m = map(int, hhmm.split(":"))
    return datetime.now(tz).replace(hour=h, minute=m, second=0, microsecond=0)


def cmd_collect(cfg: dict, args) -> int:
    from . import verify
    from .collect import collect
    from .db import DB, utcnow
    from .extract import extract_all
    from .llm import LLM

    started = utcnow()
    db = DB(cfg["report"]["database"])
    stats: dict = {}
    try:
        from .config import apply_profile
        apply_profile(cfg, profile=getattr(args, "profile", None))
        run = cfg["run"]
        cfg["llm"]["max_calls_per_run"] = run["max_ai_calls"]
        stats["profile"] = run["profile"] + ("（展會月加碼）" if run["event_month"] else "")
        stats["tavily_queries"] = len(run["tavily_queries"])
        log.info("本次蒐集：%s，回看 %s 小時，Tavily %d 次查詢，AI 上限 %d 次",
                 stats["profile"], run["lookback_hours"], len(run["tavily_queries"]), run["max_ai_calls"])
        deadline = _at_today(cfg, cfg["schedule"].get("collect_deadline", "08:35"))
        tz = deadline.tzinfo
        # 只有在「截止時間之前」啟動才套用截止時間（避免手動在深夜執行時一開始就停止）
        stop_active = datetime.now(tz) < deadline
        should_stop = lambda: stop_active and datetime.now(tz) >= deadline

        # 週日用較準確的模型；不能用時 _working_llm 會自動改回預設模型
        if run.get("extract_model"):
            base = cfg["llm"]["extract_model"]
            cfg["llm"]["fallback_models"] = [base] + [m for m in cfg["llm"].get("fallback_models", []) if m != base]
            cfg["llm"]["extract_model"] = run["extract_model"]
        llm = _working_llm(cfg, stats)
        if llm is None:
            log.error("AI 無法使用，今天跳過萃取。原因：%s", stats["llm_probe"])
            return 1

        if run["collect_news"]:
            # Tavily 月用量保護：快到免費上限時減少或停止新聞搜尋
            hard_cap = (cfg.get("collection") or {}).get("tavily_monthly_hard_cap", 950)
            remaining = max(0, hard_cap - db.usage("tavily"))
            if len(run["tavily_queries"]) > remaining:
                log.warning("Tavily 本月剩 %d 次，新聞搜尋從 %d 次減為 %d 次", remaining, len(run["tavily_queries"]), remaining)
                run["tavily_queries"] = run["tavily_queries"][:remaining]
            src_errors: dict = {}
            articles, src_stats = collect(cfg, errors=src_errors)
            stats["sources"] = src_stats
            stats["source_errors"] = src_errors
            stats["after_keyword_filter"] = len(articles)
            articles = verify.dedupe_articles(articles, db)
            stats["after_dedupe"] = len(articles)
            # 官方與評測優先送 AI，其次是有全文的文章
            rank = {"official": 0, "review": 1, "media": 2, "aggregator": 3, "social": 4}
            articles.sort(key=lambda a: (rank.get(a.source_type, 5), a.content_level != "full"))
            results, ex_stats = extract_all(articles, llm, cfg["llm"], should_stop)
            processed = ex_stats.pop("processed")
            stats["extract"] = ex_stats | {"llm_calls": llm.calls}
            stats["ingest"] = verify.ingest(db, results)
            for a in processed:
                db.add_article(a.url, a.title, a.source)
            db.add_usage("tavily", run.get("tavily_used", 0))

        # 規格補完：用剩下的 AI 額度補查缺少規格的機型
        if run.get("enrich_models"):
            from .enrich import enrich
            try:
                stats["enrich"] = enrich(db, cfg, llm, run["enrich_models"], should_stop)
            except Exception as exc:
                log.exception("規格補完失敗（不影響已完成的蒐集）")
                stats["enrich"] = {"error": f"{type(exc).__name__}：{exc}"}
        stats["llm_calls_total"] = llm.calls
        stats["tavily_month_total"] = db.usage("tavily")
        log.info("完成：%s", stats.get("ingest", {}))
        return 0
    finally:
        db.log_run("collect", started, stats)
        db.close()
        _print_stats(stats)


def _working_llm(cfg: dict, stats: dict):
    """先用極短請求測試 AI；設定的模型不能用時，依序試 fallback_models。"""
    from .llm import LLM
    llm_cfg = dict(cfg["llm"])
    candidates = [llm_cfg["extract_model"]] + [m for m in llm_cfg.get("fallback_models", []) if m != llm_cfg["extract_model"]]
    probes = {}
    for model in candidates:
        llm_cfg["extract_model"] = model
        try:
            llm = LLM.from_config(llm_cfg)
        except Exception as exc:
            probes[model] = f"{type(exc).__name__}：{exc}"
            break  # 金鑰缺失之類的問題，換模型也沒用
        result = llm.probe()
        probes[model] = result
        log.info("AI 測試 %s：%s", model, result)
        if result == "ok":
            stats["llm_probe"] = probes
            stats["llm_model"] = model
            if model != cfg["llm"]["extract_model"]:
                log.warning("設定的模型 %s 無法使用，改用 %s。建議到 config.yaml 更新 extract_model", cfg["llm"]["extract_model"], model)
            return llm
        if "BudgetExceeded" in result:
            break
    stats["llm_probe"] = probes
    return None


def _print_stats(stats: dict):
    src = stats.get("sources", {})
    if src:
        zero = [k for k, v in src.items() if v == 0]
        log.info("來源統計：共 %d 個，%d 個今天沒抓到文章%s", len(src), len(zero), f"（{', '.join(zero[:8])}）" if zero else "")
    for name, err in stats.get("source_errors", {}).items():
        log.info("來源失敗：%s → %s", name, err)
    for k in ("profile", "llm_probe", "after_keyword_filter", "after_dedupe", "extract", "ingest", "enrich"):
        if k in stats:
            log.info("%s：%s", k, stats[k])


def cmd_report(cfg: dict, args) -> int:
    from .db import DB, utcnow
    from .llm import LLM
    from .mailer import send_report
    from .report import build_report

    started = utcnow()
    db = DB(cfg["report"]["database"])
    try:
        try:
            llm = LLM.from_config(cfg["llm"], role="summary")
        except Exception as exc:
            log.warning("AI 摘要不可用，改用規則摘要：%s", exc)
            llm = None
        ctx, html, xlsx, ids = build_report(db, cfg, llm)
        log.info("報告已產生：%s", ctx["subject"])

        if args.dry_run:
            log.info("dry-run：不寄信。報告在 %s", Path(cfg["report"]["output_dir"]).resolve())
            return 0

        send_at = _at_today(cfg, cfg["schedule"].get("send_at", "09:30"))
        wait = (send_at - datetime.now(send_at.tzinfo)).total_seconds()
        if not args.no_wait and 0 < wait < 3 * 3600:
            log.info("等待 %.0f 分鐘，%s 準時寄出", wait / 60, send_at.strftime("%H:%M"))
            time.sleep(wait)
        send_report(ctx["subject"], html, [xlsx])
        db.mark_reported(ids)
        db.log_run("report", started, {"events": len(ids), "subject": ctx["subject"]})
        return 0
    except Exception as exc:
        log.exception("報告失敗")
        _notify_failure(f"報告產生或寄送失敗：{exc}")
        return 1
    finally:
        db.close()


def _notify_failure(message: str):
    """失敗時寄一封簡短通知給寄件帳號本人。"""
    try:
        from .config import env
        from .mailer import send_report
        user = env("SMTP_USER")
        if user:
            send_report("[顯示器情報] 執行失敗通知", f"<p>{message}</p><p>請到 GitHub 的 Actions 頁面查看詳細紀錄。</p>", [], [user])
    except Exception:
        log.exception("連失敗通知都寄不出去")


def cmd_run(cfg: dict, args) -> int:
    try:
        cmd_collect(cfg, args)
    except Exception:
        log.exception("蒐集階段失敗，仍繼續產生報告")
    return cmd_report(cfg, args)


def cmd_check_sources(cfg: dict, args) -> int:
    from .collect import check_sources
    results = check_sources(cfg)
    for name, status, note in results:
        print(f"{status:4}  {name}  {note}")
    bad = [r for r in results if r[1] != "OK"]
    print(f"\n共 {len(results)} 個來源，{len(results) - len(bad)} 個正常，{len(bad)} 個需要處理")
    return 0


def cmd_test_email(cfg: dict, args) -> int:
    from .mailer import send_report
    send_report("[顯示器情報] 測試信", "<p>收到這封信，代表寄信設定成功。</p>", [])
    print("測試信已寄出，請檢查收件匣（也看看垃圾郵件）。")
    return 0


def cmd_demo(cfg: dict, args) -> int:
    from .demo import run_demo
    path = run_demo(cfg)
    print(f"範例報告已產生：{path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(prog="display_intel", description="筆電顯示器情報 agent")
    p.add_argument("command", choices=["collect", "report", "run", "check-sources", "test-email", "demo"])
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true", help="不寄信，只產生報告檔案")
    p.add_argument("--no-wait", action="store_true", help="不等到寄送時間，立刻寄出")
    p.add_argument("--profile", choices=["auto", "light", "full", "enrich"], default="auto",
                   help="蒐集模式：auto 依星期自動；light 輕量；full 完整；enrich 只補查缺少的規格")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    handler = {"collect": cmd_collect, "report": cmd_report, "run": cmd_run,
               "check-sources": cmd_check_sources, "test-email": cmd_test_email, "demo": cmd_demo}[args.command]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
