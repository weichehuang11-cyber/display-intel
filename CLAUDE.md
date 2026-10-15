# CLAUDE.md

給接手這個專案的 Claude Code（或工程師）的說明。

## 專案目的

定期自動蒐集全球 Notebook／Laptop／AIO 顯示螢幕規格（LCD、OLED）、平台廠顯示技術、面板廠技術能力與世代線並寄出報告。目前為**週報**：每週日台灣時間 22:00 寄出（`config.yaml` 的 `schedule.cadence` 可切換 weekly / daily）。主要用途：市場情報與技術規格收集。

使用者沒有程式背景：**所有說明、log、報告、錯誤訊息都用繁體中文**；設定集中在 `config.yaml`，讓非工程人員能在 GitHub 網頁上修改。

## 架構

```
collect.py  蒐集：RSS、Google 新聞 RSS（多語系）、Tavily（選用）→ 關鍵字初篩 → 補抓全文
verify.py   去重（網址＋標題相似度）→ AI 萃取後合併到資料庫、可信度、傳聞追蹤
extract.py  萃取：多篇文章一批送 AI，輸出 JSON；回來後逐字比對原文依據
derive.py   衍生規格（PPI、長寬比、像素密度）、合理性檢查、原文依據檢查
llm.py      AI 介面：gemini（預設免費）/ anthropic，內建節流與額度保護；GitHub Models 已於 2026-07-30 停止服務
db.py       SQLite（data/intel.sqlite，由 GitHub Actions commit 回 repo）
report.py   未報告事件 → HTML Email（templates/report.html.j2，卡片式、手機友善）＋ Excel
analytics.py 規格完整度（13 項重點欄位，含觸控）、關鍵數字、亮點、週對週、技術提及率、品牌×面板廠、傳聞追蹤、名詞辭典（全部程式計算，不經 AI）
enrich.py   規格補完（以機型為單位）：完整度未達 enrich_target 的機型，依 enrich_schedule_days 回訪；每次評測網站＋官方規格各搜尋一次 → focus 萃取 → 合併；有面板型號時反查面板原廠規格（只填空白，記在 panel_spec_fields）；Tavily 月用量記在 db.usage，達 enrich_monthly_cap 停止
            關注機型（watchlist）：watch_due 依 watch_state 表排程 → search_watch（中文機型用中文查詢）→ focus 萃取 → ingest(tag="watch")；報告中 watch 建檔的 new 事件不算新機
ihv.py      平台適配性：cpu/gpu 廠商推算、五項評比（✓/✗/?/—，未知不等於不支援）、平台 × 面板對照表
panel_decode.py 面板型號前綴 → 面板廠與尺寸（標示「推算」）、面板廠名稱統一
mailer.py   Gmail SMTP（應用程式密碼）
__main__.py 總控：collect / report / run / check-sources / test-email / demo
demo.py     虛構範例資料，不需金鑰即可產生報告
```

排程（`.github/workflows/`）：`collect.yml` 週一、三、五、日 19:17 台灣時間（一三五 light、週日 full、手動可選 enrich，見 `config.yaml` 的 `collection` 與 `config.apply_profile`）；`report.yml` 週日 21:17 啟動、等到 22:00 寄出。兩者共用 concurrency group `database` 避免同時寫入。NAS 用 `Dockerfile` + `docker-compose.yml`，指令 `run`。

## 核心規則（修改時務必維持）

1. **防幻覺**：每個規格欄位都必須有逐字出自文章的 evidence，否則 `derive.enforce_evidence` 會刪除。不要放寬這條規則。
2. **衍生規格由程式計算**，不從文章抄。
3. **可信度**：A 官方、B 實測、C 媒體／彙整、D 傳聞、E 待確認（不進報告）。低可信度來源不能覆蓋高可信度數值；D 被 C 以上證實時產生 `rumor_confirmed` 事件。
4. **單一來源或單批失敗不中斷流程**；額度用完（`BudgetExceeded`）時停止萃取，用已完成的部分出報告。
5. **排定時間一定要寄出**：報告失敗時寄失敗通知給 SMTP_USER。
6. 事件寄出成功後才 `mark_reported`；dry-run 不標記。

## 開發

```bash
pip install -r requirements.txt
export PYTHONPATH=src
python -m pytest -q          # 全部測試不需網路與金鑰
python -m display_intel demo # 產生 output/report-日期.html
```

新增功能時同步加測試到 `tests/test_core.py`。

## 已知限制與待辦

- 同款機型合併：`verify.compatible` 以品牌＋正規化機型＋相容的尺寸/解析度/面板技術判斷，每次 ingest 後執行 `consolidate`
- 可信度目前是「整筆紀錄」層級，設計文件要求「欄位層級」（每個欄位各自記來源與等級）
- Google 新聞連結是轉址頁，抓不到全文，只有摘要
- 社群只有 Reddit RSS；X、微博需付費 API 或第三方服務
- 面板廠世代線尚未有獨立的能力檔案資料表（目前以 fab 事件記錄），每月對照表未實作
- 爆料者命中率統計未實作
- 報告尚未內嵌圖表；網頁版資料庫（GitHub Pages）未實作

設計文件（完整 Schema、來源清單、路線圖）在使用者的 claude.ai「筆電顯示器情報 AI Agent 系統設計」文件中。
