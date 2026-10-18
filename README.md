# Display Intel：筆電顯示器情報 Agent

定期自動蒐集全球 Notebook、Laptop、AIO 的螢幕規格（LCD／OLED）、平台廠（Intel、NVIDIA、AMD、Qualcomm）顯示技術、面板廠世代線動態，每週日台灣時間 **22:00** 把整理好的週報寄到你的信箱，附完整 Excel 規格表。（也可改回每天寄日報，見「日常調整」）

- 全部在 GitHub 雲端執行，**你的電腦不用開機**
- 預設**完全免費**：GitHub Actions＋Google Gemini 免費 AI＋Gmail
- 每個數值都經過原文比對，AI 讀錯或亂編的數字會被自動刪除

---

## 第一次設定（約 20 分鐘，全部在網頁上完成）

> 不需要寫程式。照著做，遇到畫面和說明不一樣，截圖問 Claude 即可。

### 步驟 1：申請 Gmail「應用程式密碼」

報告會從你的 Gmail 寄出。Google 不允許程式直接用登入密碼，需要另外產生一組專用密碼。

1. 用電腦打開 <https://myaccount.google.com/security>
2. 確認「**兩步驟驗證**」是「開啟」。如果是關閉，先點進去開啟（需要手機）
3. 打開 <https://myaccount.google.com/apppasswords>
4. 「應用程式名稱」輸入 `display-intel`，按「**建立**」
5. 畫面會出現 16 個英文字母（例如 `abcd efgh ijkl mnop`），**複製下來**，等一下要用。這組密碼只會顯示一次

### 步驟 2：把金鑰存進 GitHub（Secrets）

Secrets 是 GitHub 的保險箱，存進去後任何人（包括你自己）都看不到內容，只有程式執行時能用。

1. 打開你的 repo 頁面（網址像 `https://github.com/你的帳號/display-intel`）
2. 點上方的 **Settings**（齒輪圖示，在最右邊）
3. 左側選單找到 **Secrets and variables** → 點 **Actions**
4. 點綠色按鈕 **New repository secret**，依序新增以下四個（每個新增完按 **Add secret**）：

| Name（名稱，要一模一樣） | Secret（內容） |
| --- | --- |
| `SMTP_USER` | 你的 Gmail 地址，例如 `abc@gmail.com` |
| `SMTP_PASSWORD` | 步驟 1 複製的 16 個字母（空格可留可不留） |
| `REPORT_RECIPIENTS` | 收報告的信箱，多個用英文逗號分隔，例如 `abc@gmail.com,boss@company.com` |
| `GEMINI_API_KEY` | Google Gemini 免費 AI 金鑰，申請方式見下方 |

**申請 Gemini 免費金鑰（約 3 分鐘）**
1. 打開 <https://aistudio.google.com/apikey>，用 Google 帳號登入，同意使用條款
2. 點「**Create API key**」（建立 API 金鑰），專案可選預設的那一個
3. 複製出現的金鑰（`AIza` 開頭的一長串），存成上表的 `GEMINI_API_KEY`
4. 不需要綁信用卡；免費版每日次數有限，程式已經自動控制用量

### 步驟 3：確認 Actions 已開啟

1. 點 repo 上方的 **Actions** 分頁
2. 如果看到「Workflows aren't being run on this repository」或綠色按鈕「**I understand my workflows, go ahead and enable them**」，點它
3. 左側應該看到三個項目：`1. 定期蒐集`、`2. 每週報告`、`測試`

### 步驟 4：第一次手動測試

**4-1 測試寄信與報告格式（不寄信，約 2 分鐘）**

1. Actions → 左側點 **2. 每週報告** → 右側點 **Run workflow** → mode 選 `test` → 按綠色 **Run workflow**
2. 等約 1–2 分鐘，列表出現綠色勾勾 ✓ 代表成功
3. 點進那一次執行 → 頁面最下方 **Artifacts** → 下載 `report-...`，解壓縮後用瀏覽器打開 `report-日期.html`

（第一次資料庫是空的，報告會顯示「今日沒有新消息」，這是正常的。）

**4-2 跑一次真正的蒐集（約 20–40 分鐘）**

1. Actions → **1. 定期蒐集** → **Run workflow** → **Run workflow**
2. 執行中是黃色圓圈，可以關掉網頁，晚點再回來看
3. 完成後點進去 → 點 `collect` → 展開「蒐集、萃取、查核」，最後幾行會顯示抓到幾篇文章、新增幾筆規格

**4-3 立刻寄一封真的報告**

1. Actions → **2. 每週報告** → **Run workflow** → mode 選 `now` → **Run workflow**
2. 幾分鐘後檢查信箱（第一次可能在「垃圾郵件」，請標示為「非垃圾郵件」）

### 步驟 5：完成！

之後會自動執行，不用再做任何事：

| 台灣時間 | 動作 |
| --- | --- |
| 週一、三、五 19:17 | 輕量蒐集最近 2 天的新聞、AI 萃取規格，再補查 6 款缺規格的機型（不寄信） |
| 週二 19:17 | **規格補完日**：不蒐集新聞，用整天的 AI 額度補查最多 30 款缺規格的機型 |
| 週日 19:17 | 完整蒐集過去 7 天的新聞，補查 3 款機型（最晚 21:35 收工） |
| 週日 21:17 | 產生週報，等待到 22:00 |
| 週日 22:00 | 寄出週報 |

分散在一週中蒐集的好處：AI 免費額度每天重置，分散執行可以處理更多文章（週日完整蒐集會用掉當天大部分額度，所以大量補完放在週二）；只保留幾天的新聞也比較不會漏掉。展會月（1 月 CES、5–6 月 Computex、9 月 IFA）會自動多搜尋一些。

**規格補完**（目標：上市滿 30 天的機型完整度 75%）：新聞稿常只寫一部分規格，面板廠、面板型號、反應時間、調光方式通常要等深度評測才公開。每次蒐集後，系統會挑出完整度未達 10/13 的機型：

- 每款搜尋 2 次：深度評測網站（Notebookcheck 等）＋ 官方規格文件（品牌官網、Lenovo PSREF、HP 規格表）
- **面板型號**：程式直接掃描評測頁原文中的面板型號與 EDID 識別碼（例如 `BOE NE160QDM-NZ1`、`BOE0B8E`），不花 AI 額度；還缺的機型會專門到 Notebookcheck、LaptopMedia 等評測網站搜尋一次。依型號推算的尺寸對應到正確的螢幕選配，對應不明確（例如同尺寸有兩種面板）時寧可留空，不猜
- 查到面板型號後，就能推算面板廠（標 `*`），再查該面板的原廠規格補上空白欄位（報告會標示「面板原廠規格」）
- 回訪時間：發現時立刻查，之後在上市第 14、35 天再查，配合評測出現的時間；達到 10/13 就停止
- 補到的內容會出現在週報的「規格更新」；報告開頭的「上市滿 30 天完整度」就是追蹤 75% 目標的指標

## 關注機型清單

新聞只會帶進「這週有消息」的機型。想長期追蹤的主力機型（例如 LG gram 17、機械革命蛟龍系列），可以放進 `config.yaml` 的 `watchlist`，不論有沒有新聞都會：

- 主動搜尋官方規格頁與評測，建立完整規格紀錄（中文機型用中文搜尋）
- 每月回查一次，抓新款與規格更新；也會參與規格補完與平台適配評比
- 週報有獨立的「關注機型」區塊，不會混進「新機型」；Excel 附件有「關注機型」工作表

新增一款：在 `watchlist:` 底下照格式加一行，例如 `- {brand: Samsung, model: Galaxy Book Pro}`。中文機型可加 `aliases`（其他寫法）與 `lang: zh`。每次蒐集最多建檔 5 款（週日 10 款），清單上 26 款約 2 週內全部建檔完成。

## 週報版面與比較表

週報由上而下：關鍵數字 → 重點與亮點 → **新機規格比較表**（依尺寸分段：13 吋以下／14／15.6／16／17–18 吋／AIO）→ 規格更新、傳聞證實 → **關注機型比較表** → 平台與產線 → 市場分析與季度趨勢 → 傳聞 → 附錄「新機詳細規格」卡片。

- 比較表同一段內各欄最佳值以**綠底**標示（刷新率、反應時間、SDR／HDR 亮度、DCI-P3、PPI），同一款機型的選配用同一底色
- `*` 表示面板廠由面板型號推算；「規格 7/13」為 13 項重點規格已知幾項（紅色＝不到一半）
- 手機上表格可左右滑動
- Excel 附件第一個工作表「比較表」：欄位分群（基本／動態／亮度與色彩／表面與光學／結構／機構／介面／面板來源），凍結機型欄，綠底標最佳值，最後一欄可直接點開來源

## 資料正確性怎麼把關、系統壞了怎麼知道

**每一個數字都有四道關卡：**

1. **逐字原文依據**：AI 填的每個規格都必須附上文章裡的原句，程式逐字比對，對不上就刪掉（防止 AI 編造）
2. **合理範圍**：例如亮度超過 2000 nits、刷新率超過 540Hz、HDR 比 SDR 還低，直接刪除
3. **可信度分級（每個欄位各自記錄）**：官方 A＞實測 B＞媒體 C＞傳聞 D。每個數字都記得是哪個網站、哪個等級提供的；低等級來源不能覆蓋該欄位的高等級數值，同等級但數值不同會列出兩個網站
   - 週報詳細卡片有「出處」行（依網站列出各自提供了哪些欄位）；比較表上的小字 `傳`＝傳聞、`面`＝面板原廠規格、`✎`＝人工更正
   - Excel「比較表」滑鼠移到數值上可看出處、原文與佐證網站；「欄位來源」工作表列出每一個數字的出處
4. **面板型號反查**：面板廠、尺寸可由面板型號推算交叉比對（標 `*`）

**週報最後的「系統健康與資料查核」**（標題下方會先顯示 🟢／🟡／🔴 總燈號）：

| 檢查 | 看什麼 |
|---|---|
| 排程執行 | 該跑的蒐集有沒有都跑 |
| 新聞來源 | 哪個網站整週連不上或都是 0 篇 |
| AI 萃取 | 失敗比例、是否因額度提前停止、是否自動換了備用模型 |
| Tavily 額度 | 本月用量是否超前月份進度 |
| 資料量 | 文章數是否突然比前幾週少很多 |
| 原文依據 | 本期每個欄位是否都有原句 |
| 自動攔截 | AI 填錯被程式刪掉的比例（太高代表 AI 或來源有問題） |
| 多來源佐證 | 重點規格有幾成已被第二個獨立網站寫出相同數值 |
| 來源不一致 | 不同網站對同一款螢幕寫了不同數值（只看螢幕規格） |
| 跨欄位矛盾 | 例如 OLED 卻有 Mini LED 分區、DisplayHDR 600 但峰值只有 400、面板型號推算的尺寸和機型不符 |
| 重複機型 | 名稱很接近、可能沒合併的機型 |
| 抽樣稽核 | 每週隨機 5 款，列出「數值＋原文句子＋來源連結」，花 1 分鐘點開核對 |

Excel 附件有完整的「資料查核」工作表。如果「重複機型」提醒的兩款其實是不同機型，把名稱加到 `config.yaml` 的 `health.not_duplicates`，以後就不再提醒。看到 🔴 或同一個 🟡 連續幾週出現，把那段截圖給 Claude 就能追查修正。

另外：Gemini 免費額度是各模型分開計算，一個模型當天額度用完時會自動換下一個備用模型繼續，不會整批停下。

## 人工更正（用你的專業知識修正資料）

你確認過的資訊（例如某機型實際用哪家面板、某個數字報導寫錯了），可以在 GitHub 網頁上編輯 `config.yaml` 的 `corrections`：

```yaml
corrections:
  - brand: HP
    model: OMEN 16
    match: {res_w: 2560}          # 選填：只改 2560 解析度的那個選配
    set: {panel_maker: BOE, panel_model: NE160QDM-NZ1}
    note: 供應鏈確認 2026-10     # 出處說明，會顯示在報告上
```

- 存檔後，下一次蒐集或週報就生效；週報「規格更新」會列出被更正的項目，讓你確認
- 人工更正的欄位以你的值為準，之後網路上的新聞（即使是官方）都不會覆蓋
- 某個數值確認是錯的、又不知道正確值：設成 `null`（例如 `set: {pwm_hz: null}`），會保持空白
- 機型名稱打錯找不到時，週報的「系統健康與資料查核」會提醒
- 比較表上人工更正的數值旁有小字 `✎`

## 季度趨勢（尺寸、解析度、刷新率等分布）

週報「市場分析」有季度趨勢表：各季新機在尺寸、面板技術、刷新率、解析度、長寬比、表面處理（AG／AGLR／BV／AR）、面板廠的占比，以及最近一季相對前一季的變化（百分點）。Excel 附件有完整的「季度趨勢」工作表。

- 季度依文章寫的發表／上市時間判斷，沒寫則用新聞日期；關注清單的既有機型沒有時間就不計入
- 以機型為單位（同機型多種選配取資料最完整的一筆）；每季至少 5 款才計算季變化

**歷史回溯**：為了讓季度趨勢有得比較，系統會補建 2025 Q4～2026 Q3 的資料（設定在 `config.yaml` 的 `backfill`）。

- 11/1 起每次蒐集順便回溯一點（每次最多 12 次 Tavily、每月最多 160 次），由近到遠一季一季補
- 各季鎖定當季大展：Q1 CES、Q2 Computex、Q3 IFA；另用免費的 Google 新聞補摘要
- 回溯到的機型不會出現在週報「新機型」，只進季度趨勢與 Excel；週報底部會顯示回溯進度
- 想立刻開始：「1. 定期蒐集」→ Run workflow → profile 選 `backfill`

## 進階規格（光學／結構／機構／介面）

除了基本規格，卡片會在資料有寫時顯示四行進階資訊（沒寫就不顯示，不計入完整度）：

| 分類 | 欄位 |
| --- | --- |
| 光學 | 反射率、霧度、亮度均勻度、可視角 |
| 結構 | 背板（a-Si／Oxide／LTPS／LTPO）、色深原文（10-bit、8-bit+FRC）、觸控結構（On-cell 等）、蓋板玻璃 |
| 機構 | 邊框寬度、屏佔比、面板厚度、鏡頭位置 |
| 介面 | eDP 版本、VRR 最低刷新率、面板功耗 |

這些多半只出現在深度評測與面板 datasheet；查到面板型號時，系統會反查面板原廠規格補上。

## 平台適配性評比（Intel、NVIDIA、AMD、Qualcomm）

每款機型會記錄處理器、獨立顯示卡、顯示切換方式（MUX、Advanced Optimus）與平台認證（Intel Evo、Copilot+ PC 等），並檢查螢幕與平台的搭配：

| 項目 | 看什麼 |
| --- | --- |
| 可變刷新率 | 是否標示 G-SYNC、FreeSync 或 VRR 範圍；標示 G-SYNC 但不是 NVIDIA 獨顯會被標為矛盾 |
| 獨顯直連 | 有獨顯時，是否有 MUX 或 Advanced Optimus（遊戲延遲較低） |
| 節能搭配 | 平台顯示節能技術（LRR 1.0／2.0、UBRR、DPST、OPST、PSR／PSR2、Panel Replay、DRRS、AMD Vari-Bright，保留版本號），或面板低刷新、動態刷新；標示 Intel 專屬技術但平台不是 Intel 會被標為矛盾 |
| HDR | 是否有 DisplayHDR、Dolby Vision 等標示 |
| 平台認證 | Intel Evo、Copilot+ PC、NVIDIA Studio、AMD Advantage 等 |

符號：✓ 已確認　✗ 不支援或資訊矛盾　? 未公開　— 不適用。只根據公開資料判斷，「沒寫」不會當成「不支援」，也不打主觀分數。報告的市場分析另有「平台 × 面板」對照表，Excel 附件有「平台適配」工作表。

## 臨時需求：手動加跑

Tavily 用到 850 次時會自動暫停補完，保留至少 150 次給臨時需求：

| 想做的事 | 操作 | 約用 Tavily |
| --- | --- | --- |
| 有大型發表會，想立刻蒐集 | 「1. 定期蒐集」→ Run workflow → profile 選 `full` | 55 次 |
| 只想補齊既有機型的規格 | profile 選 `enrich` | 每款約 2–3 次，最多 30 款 |
| 想立刻收到最新報告 | 「2. 每週報告」→ mode 選 `now` | 0 次 |

> GitHub 的免費排程在尖峰時段偶爾會延遲 10–30 分鐘，所以報告階段提早啟動預留緩衝。

---

## 日常調整（都在網頁上改 `config.yaml`）

在 repo 首頁點 `config.yaml` → 右上角鉛筆圖示 ✏️ 編輯 → 改完按 **Commit changes**。

| 想做的事 | 改哪裡 |
| --- | --- |
| 新增或刪除新聞來源 | `sources:` 底下的 `rss:` 清單，照現有格式新增一行 |
| 新增搜尋關鍵字 | `google_news:` → `queries:` |
| 讓更多文章被收進來 | `keywords:` 底下加詞 |
| 改寄送時間 | `schedule:` → `send_at`（也要同步改 `.github/workflows/report.yml` 的 cron） |
| 調整搜尋量 | `collection:` 底下 `light`（週一三五）、`enrich`（週二）與 `full`（週日）的 `tavily_queries_per_run`、`enrich_models`、`max_ai_calls` |
| 新增或刪除關注機型 | `watchlist:` 清單；每次建檔數量與回查頻率在 `collection:` 的 `watch_per_run`、`watch_refresh_days` |
| 調整補查規則 | `collection:` 的 `enrich_target`（幾項就停）、`enrich_schedule_days`（回訪時間）、`enrich_monthly_cap`（月用量上限） |
| 調整展會月 | `collection:` 的 `event_months` 與 `event_extra_queries_per_run` |
| 改回每天寄日報 | `schedule:` 的 `cadence` 改成 `daily`、`send_at` 改成 `"09:30"`、`collect_deadline` 改成 `"08:35"`、`lookback_hours` 改成 `26`；兩個 workflow 檔案開頭的註解寫了要換成的 cron |
| 改信件標題 | `report:` → `subject_prefix` |

改來源後建議手動跑一次「1. 定期蒐集」，看紀錄裡「來源統計」有沒有某個來源一直是 0 篇（代表網址失效）。

---

## 常見問題

**Q：Actions 出現紅色 ✗ 失敗怎麼辦？**
點進那次執行，展開紅色的步驟，把最後 20 行複製給 Claude 看。常見原因：
- `Gmail 登入失敗` → SMTP_PASSWORD 打錯，或沒開兩步驟驗證
- `GEMINI_API_KEY` 缺少、`400` 或 `403` → Gemini 金鑰沒存好或打錯，重新複製一次
- `429` 或「免費額度已用完」→ 當天免費次數用完，隔天自動恢復；常發生就把 `max_calls_per_run` 調小
- `404` 或 model not found → 免費模型名稱改了：到 <https://ai.google.dev/gemini-api/docs/models> 找一個 Flash 或 Flash-Lite 模型，把 `config.yaml` 的 `extract_model` 和 `summary_model` 換成新名稱

**Q：每週都會收到信嗎？沒有新消息也會寄？**
會。沒有新消息時，報告會列出「持續討論中」的近期主題。若某天完全失敗，會寄一封「執行失敗通知」到寄件信箱。

**Q：repo 要設公開（Public）還是私人（Private）？**
建議 **Public**：公開 repo 的 Actions 執行時間不限，完全免費。程式和新聞資料本來就是公開資訊，金鑰存在 Secrets 不會外洩。Private 每月只有 2,000 分鐘，可能不夠用。

**Q：60 天沒動 repo，排程會停嗎？**
GitHub 會停用長期沒有活動的 repo 排程；本程式每週會把資料庫存回 repo，算是有活動。如果收到 GitHub 的停用通知信，到 Actions 頁面按「Enable workflow」即可。

**Q：免費版能做到多完整？**
大約是付費完整版的五到六成：AI 免費額度有限，新聞量大的週可能處理不完，Google 新聞多數只有摘要，社群只有 Reddit。官方新聞、主要評測網、面板廠大新聞都能涵蓋。

---

## 建議加裝：Tavily 全文搜尋（免費，約 3 分鐘）

Google 新聞大多只給標題和摘要，Tavily 可以抓到文章全文，解析度、刷新率、亮度、面板型號等細節會多很多。免費版每月 1,000 次；新聞搜尋每週約 70 次，規格補完每款 2 次，一般月份約用 650–750 次。程式會記錄當月用量：用到 850 次自動暫停補完，950 次連新聞搜尋也停止，絕不會超過免費額度。設定頁的「Pay as you go」請保持關閉，額度用完只會暫停，不會收費。

1. 打開 <https://app.tavily.com>，用 Google 帳號登入
2. 登入後首頁會顯示一組 `tvly-` 開頭的 API Key，點複製
3. GitHub → Settings → Secrets → 新增 `TAVILY_API_KEY`，貼上金鑰
4. 下次執行「1. 定期蒐集」就會自動使用

## 升級：AI 換成 Claude（品質最有感的升級，每月約 US$20–60）

1. 到 <https://console.anthropic.com> 註冊，儲值後建立 API Key
2. GitHub → Settings → Secrets → 新增 `ANTHROPIC_API_KEY`
3. 編輯 `config.yaml`：把 `llm:` 底下「免費版」那幾行前面加 `#`，把「升級成 Claude」那幾行前面的 `#` 拿掉

其他選配：

---

## 進階：改在 NAS 執行

NAS 準時度較好、資料留在自己手上。需要 NAS 支援 Docker（Synology 叫 Container Manager，QNAP 叫 Container Station）。

1. 把整個 repo 下載到 NAS，例如 `/volume1/docker/display-intel`
2. 在該資料夾建立 `.env`，照 `.env.example` 填入 Gmail 與 Gemini 金鑰
3. Synology「控制台 → 任務排程表 → 新增 → 排程的任務 → 使用者定義的指令碼」，使用者選 root，週一、三、五、日 19:15，指令：
   ```
   cd /volume1/docker/display-intel && docker compose run --rm agent
   ```
4. 報告會存在 `output/` 資料夾並寄出

> NAS 版與 GitHub 版不要同時開啟，否則會收到兩封信。改用 NAS 後，到 GitHub Actions 把兩個每週排程按「Disable workflow」。

---

## 給開發者

```bash
pip install -r requirements.txt
export PYTHONPATH=src
python -m display_intel demo            # 用虛構資料產生範例報告，不需金鑰
python -m display_intel check-sources   # 測試來源連線
python -m display_intel collect         # 蒐集＋萃取
python -m display_intel report --dry-run
python -m pytest -q
```

架構與設計說明見 `CLAUDE.md`。
