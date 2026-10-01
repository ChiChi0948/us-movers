# 美股每日重大漲跌捕捉器（本機 + 網站版）

每個交易日抓出美股「大型股（≥100 億美元）」與「中型股（20～100 億美元）」漲跌幅前 25 名，
附相對 SPY 報酬、量比、1M/3M/6M/YTD/1Y 報酬與事件說明，並保存完整歷史。

## 檔案
| 檔案 | 用途 |
|---|---|
| us_movers.py | 主程式：抓資料、算指標、產生網站 |
| dashboard_template.html | 頁面範本（本機版與網站版共用） |
| themes.py | 產業分類規則（大分類 + 熱門主題細分類，可自行增修） |
| requirements.txt | Python 套件 |
| .github/workflows/daily.yml | GitHub 自動排程 + Pages 部署 |
| .gitlab-ci.yml | GitLab 自動排程 + Pages 部署 |

## 本機使用
    pip install -r requirements.txt
    python us_movers.py                                      # 最近一個已收盤交易日
    python us_movers.py --start 2026-01-01 --end 2026-09-01  # 回溯
    python us_movers.py --rebuild                            # 不抓資料，只重建頁面

輸出：
- data/movers_history.csv：完整歷史（要 commit 進 repo，這是唯一的資料庫）
- dashboard.html：本機單檔版，雙擊即可開
- site/：網站版（index.html + 依年份切分的 JSON），由 CI 部署，不用 commit

## 部署到 GitHub Pages
1. 建 repo，把所有檔案推上去（含 .github 資料夾）
2. Settings → Pages → Source 選 **GitHub Actions**
3. Settings → Actions → General → Workflow permissions 選 **Read and write**
4. Actions → daily-movers → **Run workflow**，第一次可在 args 填 `--start 2026-01-01 --no-news` 回溯
5. 網址：`https://<帳號>.github.io/<repo>/`

注意：免費帳號的 GitHub Pages 一律公開，任何人知道網址都能看。

## 部署到 GitLab Pages
1. 建 project，推上所有檔案（含 .gitlab-ci.yml）
2. 頭像 → Edit profile → Access tokens：建立 Personal access token（勾 write_repository）
3. Settings → CI/CD → Variables：新增 `PUSH_TOKEN`（勾 Masked）
4. Build → Pipeline schedules：新增 `30 22 * * 1-5`，時區 UTC
5. 第一次回溯建議在本機跑完再推上去（CI 免費分鐘數有限）；或 Build → Pipelines → Run pipeline，加變數 `EXTRA_ARGS`
6. 網址在 Deploy → Pages

GitLab 私人專案可以開 Pages 存取控制（Settings → General → Visibility → Pages），只有專案成員看得到。

## 事件說明：用 Claude / Gemini 對話整理（免 API）
1. 開網站 → 選日期、組別、漲/跌 → 按「📋 複製給 AI」→ 複製
2. 貼到 Claude 或 Gemini 對話（開啟網路搜尋）
3. 把回覆的 CSV 貼到 repo 的 data/events_manual.csv 最下方 → Commit
4. 會自動觸發重建（不重抓股價），1～2 分鐘後網站事件欄更新

檔案容錯：可以連 ``` 和標題列一起貼、重複貼也沒關係；同日同代號以最後一筆為準。

## AI 事件說明：API 自動版（選用）
在 CI 的 Secrets / Variables 設定：
- `GEMINI_API_KEY` 或 `ANTHROPIC_API_KEY`
- `MOVERS_ARGS` = `--llm gemini --llm-delay 6 --llm-top 10`（或 `--llm claude --llm-top 10`）

本機則用環境變數：`export GEMINI_API_KEY=...`（Windows 用 `set`）。

## 網站功能
- 單日 / 區間切換、大型股 / 中型股、漲幅榜 / 跌幅榜
- 點欄位排序（手機用排序選單），搜尋代號看歷次上榜
- 網址會記住目前畫面，可加書籤或分享
- 只下載需要的年份資料，手機也不會太慢
- 手機自動改成卡片版面

## 事件來源（依序，找到就停）
1. CNBC「Stocks making the biggest moves」盤前/盤中/前日盤後專欄
2. Motley Fool / Benzinga「Why X stock…」文章（附出版者摘要）
3. Yahoo Finance 個股新聞（只用於最近幾天）
4. Google News 財經媒體新聞
另外 SEC 8-K 公告會變成標籤（財報、高層異動、併購…）。
舊日期想用新來源重抓：`--start 2026-09-01 --refresh-news`（建議一次一個月）。

## 產業分類
大分類（半導體、軟體、硬體與網通…）＋細分類（記憶體、光通訊、資安、核能…）。
一檔可以同時有多個細分類（例如 MRVL：客製化 ASIC、網通晶片、光通訊）。
修正或新增：在 data/themes_manual.csv 加一行，例如 `MRVL,半導體,客製化 ASIC、光通訊`（多個細分類用「、」或「;」分隔），Commit 後自動重建。
網站上點分類標籤，或用「🏷 找細分類」搜尋框，可列出該主題在目前日期/區間的所有上榜股票（大型＋中型、漲＋跌）。
搜尋框支援別名（光模塊→光通訊、HBM→記憶體、散熱→資料中心電力/散熱）、簡體字與小錯字；
想加自己的叫法，改 themes.py 最下方的 ALIASES。

## 欄位
日內報酬 = 還原收盤價日報酬；相對 SPY = 日內報酬 − SPY 日報酬；
量比 = 當日量 ÷ 前 60 日均量；1M/3M/6M/1Y = 21/63/126/252 個交易日，預設算到前一日收盤（--include-day 改含當天）。

## 檢查某檔為什麼沒上榜
Run workflow 參數填 `--debug FORM,SPY`，紀錄裡會印出每檔的報酬、估算市值、分到哪一組、第幾名或沒上榜的原因。
每次執行也會印出各組漲跌前 5 名，方便快速核對。

## 資料保護
Yahoo 限流時會分輪補抓；若最新一天有報價的股票不到 90%，或 SPY 缺漏，這次就不更新網站（Actions 顯示紅叉），避免發布錯誤排行，稍後重跑即可。

## 限制
- 回溯用「現在的」股票池與股數，已下市或被併購的公司不會出現，歷史市值為估算值
- Yahoo / Nasdaq / Google 都是非官方介面，雲端主機偶爾會被限流；失敗時手動重跑即可
- 沒開 --llm 時事件欄是英文新聞標題；AI 說明也可能出錯，重要判斷請點來源確認
