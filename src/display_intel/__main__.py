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
        articles, src_stats = collect(cfg)
        stats["sources"] = src_stats
        stats["after_keyword_filter"] = len(articles)
        articles = verify.dedupe_articles(articles, db)
        stats["after_dedupe"] = len(articles)
        # 官方與評測優先送 AI，其次是有全文的文章
        rank = {"official": 0, "review": 1, "media": 2, "aggregator": 3, "social": 4}
        articles.sort(key=lambda a: (rank.get(a.source_type, 5), a.content_level != "full"))

        deadline = _at_today(cfg, cfg["schedule"].get("collect_deadline", "08:35"))
        tz = deadline.tzinfo
        # 只有在「截止時間之前不久」啟動才套用截止時間（避免手動在下午執行時一開始就停止）
        stop_active = datetime.now(tz) < deadline
        should_stop = lambda: stop_active and datetime.now(tz) >= deadline

        llm = LLM.from_config(cfg["llm"])
        results, ex_stats = extract_all(articles, llm, cfg["llm"], should_stop)
        processed = ex_stats.pop("processed")
        stats["extract"] = ex_stats | {"llm_calls": llm.calls}
        stats["ingest"] = verify.ingest(db, results)
        for a in processed:
            db.add_article(a.url, a.title, a.source)
        log.info("完成：%s", stats["ingest"])
        return 0
    finally:
        db.log_run("collect", started, stats)
        db.close()
        _print_stats(stats)


def _print_stats(stats: dict):
    src = stats.get("sources", {})
    if src:
        zero = [k for k, v in src.items() if v == 0]
        log.info("來源統計：共 %d 個，%d 個今天沒抓到文章%s", len(src), len(zero), f"（{', '.join(zero[:8])}）" if zero else "")
    for k in ("after_keyword_filter", "after_dedupe", "extract", "ingest"):
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
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    handler = {"collect": cmd_collect, "report": cmd_report, "run": cmd_run,
               "check-sources": cmd_check_sources, "test-email": cmd_test_email, "demo": cmd_demo}[args.command]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
