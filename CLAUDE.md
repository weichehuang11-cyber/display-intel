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
llm.py      AI 介面：gemini（預設免費）/ anthropic，內建節流與額度保護；429 額度用完時依 fallbacks 換模型（記在 stats.llm_switched）；GitHub Models 已於 2026-07-30 停止服務
db.py       SQLite（data/intel.sqlite，由 GitHub Actions commit 回 repo）
report.py   未報告事件 → HTML Email（templates/report.html.j2：依尺寸分段比較表＋關注機型比較表在前，詳細卡片在附錄）＋ Excel（第一頁「比較表」：COMPARE_COLUMNS 分群表頭、同段最佳值標綠）
analytics.py 規格完整度（13 項重點欄位，含觸控）、價格帶（configure_price、price_spec_matrix、季度趨勢「價格帶」；幣別判斷與合理性在 derive.clean_price）、關鍵數字、亮點、週對週、技術提及率、品牌×面板廠、傳聞追蹤、名詞辭典（全部程式計算，不經 AI）
enrich.py   規格補完（以機型為單位）：完整度未達 enrich_target 的機型，依 enrich_schedule_days 回訪；每次評測網站＋官方規格各搜尋一次 → focus 萃取 → 合併；面板型號：assign_panel_ids 以 panel_decode.find_panel_ids 掃描頁面原文（不經 AI，原文片段為 evidence，依推算尺寸對應選配、不明確不填），仍缺時 hunt_panel 到 PANEL_HUNT_DOMAINS 搜一次（panel_hunt_done）；有面板型號時反查面板原廠規格（只填空白，記在 panel_spec_fields、field_sources kind=panel_datasheet）；Tavily 月用量記在 db.usage，達 enrich_monthly_cap 停止
            關注機型（watchlist）：watch_due 依 watch_state 表排程 → search_watch（中文機型用中文查詢）→ focus 萃取 → ingest(tag="watch")；報告中 watch 建檔的 new 事件不算新機
backfill.py 歷史回溯：季度×查詢單位（backfill_state 表），Tavily start/end_date＋Google 新聞 after:/before:，ingest(tag="backfill")；報告中 backfill 事件不進本期卡片，只進季度趨勢；announced 取最早新聞日期
corrections.py 人工更正：config.yaml corrections＋corrections/ 資料夾的 .xlsx／.csv（load_files／parse_rows，表頭 SHEET_COLUMNS，週報 Excel 第二頁「人工更正」由 write_template 預填本期機型）（brand/model/match/set/note）→ field_sources kind=manual、conf A；collect 結束與 report 前都套用（idempotent），有變更才產生 updated 事件（tag=manual）；null＝確認錯誤保持空白；merge_panel 對 manual 欄位一律跳過
health.py   週報「系統健康與資料查核」：運作（排程、來源、AI／Tavily、資料量，讀 runs 表 stats）＋資料（原文依據、攔截率、多來源佐證 corroborated、來源不一致、跨欄位矛盾、重複機型、抽樣稽核）；全部程式計算，失敗不影響寄信
ihv.py      平台適配性：cpu/gpu 廠商推算、五項評比（✓/✗/?/—，未知不等於不支援）、平台 × 面板對照表
panel_decode.py 面板型號前綴 → 面板廠與尺寸（標示「推算」）、面板廠名稱統一
mailer.py   Gmail SMTP（應用程式密碼）
__main__.py 總控：collect / report / run / check-sources / test-email / demo
demo.py     虛構範例資料，不需金鑰即可產生報告
```

排程（`.github/workflows/`）：`collect.yml` 週一、二、三、五、日 19:17 台灣時間（一三五 light、週二 enrich（collection.enrich_day，避開週日 AI 額度）、週日 full、手動可選 enrich，見 `config.yaml` 的 `collection` 與 `config.apply_profile`）；`report.yml` 週日 21:17 啟動、等到 22:00 寄出。兩者共用 concurrency group `database` 避免同時寫入。NAS 用 `Dockerfile` + `docker-compose.yml`，指令 `run`。

## 核心規則（修改時務必維持）

1. **防幻覺**：每個規格欄位都必須有逐字出自文章的 evidence，否則 `derive.enforce_evidence` 會刪除；數值欄位的 evidence 裡必須真的有該數字（`derive.unsupported_values`：解析度寬高合併判斷、型號字母後的數字不算、只接受無歧義標準名稱 RES_NAMES 與色數換算 COLOR_COUNTS；新資料可用 `find_res_quote` 從全文找明寫的解析度補救；舊資料由 `verify.recheck_values` 在 `rederive_all` 時套用）。不要放寬這條規則。
2. **衍生規格由程式計算**，不從文章抄。`verify._derived`：`size_corrected`（`derive.actual_size`：整數吋＋16:9 → STANDARD_16_9，或面板型號尺寸）、`res_inferred`（`panel_decode` 的 RES_CODES，config `panel_res_codes`）；`rederive_all` 在 collect 開始與 build_report 前執行，規則更新時舊資料自動套用。
3. **可信度（欄位層級）**：A 官方、B 實測、C 媒體／彙整、D 傳聞、E 待確認（不進報告）。每個欄位記在 `PanelRecord.field_sources`（`FieldSource`：url、conf、kind=article/panel_datasheet/manual/record），讀取一律用 `rec.source_of(f)`（舊資料沿用整筆來源）。新數值只有比「該欄位目前來源」更可信才覆蓋；同等級不同值記 `來源數值不一致` flag（含兩個網域）；更可信來源證實同值時出處升級、原來源改列 `corroborated`；`kind="manual"` 永不被覆蓋。D 被 C 以上證實時產生 `rumor_confirmed` 事件。
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
- Google 新聞連結是轉址頁，抓不到全文，只有摘要
- 社群只有 Reddit RSS；X、微博需付費 API 或第三方服務
- 面板廠世代線尚未有獨立的能力檔案資料表（目前以 fab 事件記錄），每月對照表未實作
- 爆料者命中率統計未實作
- 圖表目前只有季度趨勢（Email 用表格畫 100% 堆疊橫條、Excel 原生圖表）；網頁版資料庫（GitHub Pages）未實作
- 價格只收文章寫出的上市起價；匯率為 config 中的概略值

設計文件（完整 Schema、來源清單、路線圖）在使用者的 claude.ai「筆電顯示器情報 AI Agent 系統設計」文件中。
