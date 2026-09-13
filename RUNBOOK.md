# chip-iq 每日健檢與維修手冊（RUNBOOK）

給「每天檢查一次站台、壞了就修」的人或 AI 看。目前由排程中的 Claude Code 在每個交易日隔天台北早上執行；交接後誰接手都照這份做。

站台本身的更新由 GitHub Actions 負責（`.github/workflows/daily.yml`，台北 18:17、20:47，隔天 06:00 補跑班），三道守門員沒過就不發佈。這份手冊處理的是「守門員擋下了、或排程根本沒跑成」之後的事。

## 前置條件

- 需要 `richlovegod/chip-iq` 的 **Write 權限**，才能觸發／啟用 workflow（`gh workflow run`、`gh workflow enable`）與 push。目前只有站主一人有；接手前要由站主把接手的人加成 collaborator，或由站主發起把 repo 轉移到公司帳號、公司帳號接受（轉移後 Pages 網址會變，見 README）。沒有 Write 權限時只做得了唯讀檢查（clone、看 run log、跑守門員），第三節的重新觸發與第四節的 push 都會被拒。
- `gh auth login` 的帳號要有 `repo` scope；改到 `.github/workflows/` 底下的檔案時，push 另需 `workflow` scope。
- 本機先 `git clone https://github.com/richlovegod/chip-iq`，以下指令都在 repo 根目錄執行。
- 排程失敗的通知信，GitHub 寄給「最後修改 workflow 檔裡 cron 的人」（workflow 被停用後又重新啟用的話，改寄給按下啟用的人），目前是站主。接手後要由接手者的帳號改一次 cron 並 push，通知才會改寄給接手者，不改的話排程失敗時接手的人收不到信。改晚上班其中一個的分鐘數即可（例如 `17 10` 改成 `18 10`）；**不要動 `0 22 * * 1-5` 那一行**，check job 是用這個字串認出早上補跑班的，改了它早上班會每天都照跑、不再略過。

## 一、什麼叫「正常」

以下標準以台北週二～週六早上（交易日隔天）檢查為前提。週日、週一檢查時，`generated_at` 超過 24 小時、過去 24 小時沒有排程都屬正常；`verify_site.py` 的 S2（`updated_at` 不是今天或昨天）也可能 FAIL。這三項改看「上週五晚班或週六 06:00 班是否成功、資料是否到預期資料日（通常是上週五；週五休市時再往前推）」，不要據此修程式或放寬 S2。

- **預期資料日** ＝ 台北今天之前最近的一個週一～週五（遇休市日往前推，休市日以第三節的 TWSE 休市表為準）
- 線上下列資料的最新日期都等於預期資料日：
  - `quote_daily.json` 的 series 最後一筆
  - `broker_daily.json` 的 `date_to`
  - `universe.json` 與 `peers.json` 的 `as_of`
  - `partners.json` 各家的 `as_of`（日本、韓國休市日與台灣不同，差 1 個交易日可接受）
- `meta.json` 的 `generated_at` 在過去 24 小時內
- 過去 24 小時的排程至少一次 success
- workflow 是 active（GitHub 會停用連續 60 天沒活動的 repo 的排程）

## 二、檢查步驟（照順序）

1. **本機 repo 狀態**：`git status`。有 `data/`、`cache/` 以外的未提交修改 → 有人正在改程式，**不要 pull、不要改**，只做唯讀檢查並記錄。只有 `data/`、`cache/` 有未提交修改或新檔（例如本機跑過 fetch 腳本）→ 資料以線上為準，先 `git stash push -u -- data cache`（要加 `-u`，才會收 `cache/` 裡的未追蹤新檔）再 `git pull --rebase origin main`；不先收起來，pull 會直接拒絕（`cannot pull with rebase: You have unstaged changes`），略過 pull 的話第 6 步驗的就是本機資料而不是線上資料。stash 事後確認用不到再清，不要用刪除 `cache/` 的方式處理。乾淨 → `git pull --rebase origin main`。
2. **時間一律用台北時間**。Windows 的 Git Bash 沒有時區資料，`TZ=Asia/Taipei date` 會默默回傳 UTC；用 Python：`datetime.now(timezone(timedelta(hours=8)))`。
3. **排程紀錄**：`gh run list --workflow=daily.yml --limit 12 --json databaseId,event,status,conclusion,createdAt`，createdAt 換成台北時間。班次對應：前一交易日 18:17 之後的第一個 schedule run ＝ 18:17 班、第二個 ＝ 20:47 班。06:00 班要看 check job 的**實際輸出行**：`gh run view <id> --log | grep -v '36;1m' | grep -E '^check\s.*(不是早上補跑班|略過|補跑|不是機器人)'`。`--log` 會把腳本原文（帶 `36;1m` 色碼）整段印出來，不排除的話每一班的 log 都查得到這幾個字；開頭的 `^check` 也不能省，update job 裡的腳本同樣可能印出「略過」（例如 `fetch_peers.py` 的「查無基本資料，略過」），混進來會把晚班誤判成 06:00 班。實際輸出「不是早上補跑班，直接跑」＝ 晚上兩班或手動觸發（手動的 event 是 `workflow_dispatch`）；其餘三種（…略過／…補跑／HEAD 是「X」的 commit…跑）＝ 06:00 班。不要用 createdAt 判斷班次：晚班可能被延遲到隔天 06:00 之後才建立。
4. **有 queued／in_progress 的 run** → `gh run watch <id> --exit-status` 等它跑完再判斷（一次約 7～10 分鐘），不要另外觸發。
5. **線上資料**：`curl -H "Cache-Control: no-cache"` 抓 `https://richlovegod.github.io/chip-iq/data/{meta,quote_daily,broker_daily,universe,peers,partners}.json`，比對第一節。
6. **本機跑守門員**（pull 之後本機資料就是線上資料）：
   ```bash
   python scripts/verify_broker.py
   python scripts/verify_partners.py
   python scripts/verify_partners.py --self-test
   python scripts/verify_site.py
   ```
7. **workflow 狀態**：`gh workflow list --all`（不加 `--all` 時，被停用的 workflow 根本不會列出來，看起來只像 workflow 不見了）。清單上顯示的是名稱「每日資料更新」（即 `daily.yml`），它不是 active → `gh workflow enable daily.yml`。

## 三、判斷與處理

| 狀況 | 處理 |
|---|---|
| 全部正常 | 只記一行 |
| 有失敗的 run，但之後有一班成功、資料是新的 | `gh run view <id> --log-failed` 記下原因。已知暫時性原因 → 不動 |
| 資料沒到預期資料日，最近的 run 都失敗 | 讀失敗 log 分類。暫時性 → `gh workflow run daily.yml`，`gh run watch` 到跑完，再驗一次線上 |
| 資料沒到預期資料日，run 都成功 | 先查是不是休市日：抓 `https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule`（只列當年度；`Date` 是民國格式，例：`1150925`），那天在表內、而且 `Name` 不含「開始交易」「最後交易」→ 休市 → 正常。**不要用 TPEx `dailyList` 判斷休市**：查休市日或檔案還沒出來的日子，它都會退回上一個交易日的清單（回應最上層的 `date` 不等於查詢日），所以一定看得到 EMdss004，照「沒有 EMdss004 才是休市」永遠判不出休市；`date` 不等於查詢日也只代表「那天沒有檔案」，可能是休市、也可能是還沒出檔，不能單靠它判成正常。不在休市表內 → 當異常查 |
| 失敗原因是程式問題（守門員誤擋、來源格式改了、解析錯、Actions 版本淘汰） | 依第四節修程式 |
| 守門員正確擋下了錯的資料（來源真的給錯） | **不要放寬守門員。** 查來源，能換端點或修解析就修；不能就記錄 🔴 等人 |

**已知的暫時性原因**（log 裡的關鍵字）：`暫停使用`（TWSE 尖峰時段停用全市場查詢）、`HTTP Error 5xx`、`IncompleteRead`、`TimeoutError`、`JSONDecodeError`、`比上一版 … 舊`（Yahoo 偶發回舊資料，程式已自動沿用上一版）、`Connection refused`／`Connection reset`／`Temporary failure in name resolution`（上游連線層失敗，`_http.py` 共嘗試 5 次、等了 76 秒後仍失敗）。同一個 URL 連續兩班以上都這樣、或訊息是 `CERTIFICATE_VERIFY_FAILED`、`Name or service not known` → 不算暫時性，當來源或程式問題查；不要只看到 `URLError` 就當暫時性一直重觸發，那會拖過「修兩次就停手」的判斷時機。

**重新觸發的時間限制**：台北 **13:30～14:30 不要觸發**（TWSE 停用全市場查詢，實測到 14:05 仍擋）。盤中到 18:00 前觸發會拿到日期不一致的快照，全站檢查只警告、照常發佈，可以接受但不理想。

## 四、修程式的規則

**可以做**：修抓資料腳本、守門員、前端的 bug；補回漏抓的資料（跑對應的 fetch 腳本）；GitHub Actions 版本被淘汰時升級。

**修完必須全部做到才能 push**：

1. 第二節第 6 步的四道檢查全過。
2. **改到守門員時**，要用情境測試證明兩件事：誤擋的情況現在通過、**真的錯誤仍然擋下**（複製一份資料、注入錯誤、跑檢查、還原）。範例：9caa48d（C11 對 Yahoo 回 0 的誤擋）與 681e767（C11 改合理視窗帶）這兩個 v0.4.2 commit 的訊息都列了情境實測，包含「真的不符仍然擋下」，看 `git show 9caa48d 681e767`。另一種做法見 909feb9（v0.6.2）：C9 擋得正確、但會連帶讓整站停發時，不改守門員，改上游抓取沿用上一版。
3. 版本號三處一起改：`data/versions.json` 最前面加一筆（只修 bug → 第三位進位；寫症狀、原因、修法）、`index.html` 頁首與頁尾的版本號。
4. commit 訊息寫「為什麼」，不只寫改了什麼。
5. push 後手動觸發一次（避開 13:30～14:30），跑到綠，確認線上資料與版本號。

**不可以做**：

- **為了讓檢查通過而放寬守門員**——除非第 2 點能證明那是誤擋
- 手動改 `data/*.json` 的數字，或手動 commit 資料假裝更新過
- 改 `data/partners_ref.json` 的股數與公司事件（人工查證維護），改黃金樣本（`broker_fixture.json`、`verify_partners.py` 的 GOLDEN／GOLDEN_CAP）
- 刪 `cache/`、force push、改寫 git 歷史
- 寄信或對外發訊息

**同一個問題修兩次還是失敗 → 停手**，把兩次嘗試與 log 記錄下來，標 🔴 等人。需要人拍板的事（換資料源、改排程架構、要不要外部觸發）只寫建議，不自己做。

## 五、紀錄

每次都記：日期（台北）、結論（✅ 正常／🟡 有狀況已處理／🔴 需要人）、前一交易日各班的實際開跑時間與結果、線上資料日。有處理的另寫：症狀、原因、做了什麼、commit 或 run 連結。紀錄放在哪由執行者的排程設定指定。
