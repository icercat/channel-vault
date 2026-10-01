# Channel Vault v3 — 可愛影音庫 / 即時直播錄製 / MP3 留存

自訂、可自行架設的多來源 Docker Compose 影音庫專案。包含後端、繁體中文 Web UI、SQLite 持久化任務佇列和 yt-dlp 自動更新，無須另外架資料庫。

ZIP 已預先建立空的 `data/`、`downloads/`、`cookies/`，解壓縮即可使用。

## 啟動

解壓縮後進入 `channel-vault` 目錄。需要 Docker 與 Docker Compose v2。

Linux / NAS：

```bash
cp .env.example .env
mkdir -p data downloads cookies
nano .env
# 將 WEB_PASSWORD 改成自己的密碼後啟動
docker compose up -d --build
docker compose logs -f --tail=100
```

Windows PowerShell（Docker Desktop 使用 Linux containers）：

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data,downloads,cookies
notepad .env
# 儲存自己的 WEB_PASSWORD 後啟動
docker compose up -d --build
```

開啟 `http://伺服器IP:8088`，本機則是 `http://localhost:8088`。
登入帳號預設 `admin`，密碼為 `.env` 的 `WEB_PASSWORD`。瀏覽器會顯示 Basic Auth 登入視窗。

第一次 build 會安裝 FFmpeg、Deno 和最新 stable yt-dlp，需要連線 Docker Hub、Debian 套件來源及 PyPI。之後每次啟動及每 24 小時會重新檢查 yt-dlp。沒有更新或更新失敗時保留原版本；已有錄製不會被更新流程停止。

## 功能與操作

- **訂閱頻道**：輸入 YouTube 頻道、Twitch 頻道或 X 帳號首頁；可填入自訂訂閱名稱。
- YouTube 來源立即安排掃描 `/videos`、`/shorts`、`/streams` 的全部可取得項目，然後每 6 小時重新掃描。影片 ID 在同一影音庫內去重；不同平台有獨立鍵，避免 ID 撞號。
- 直播監控另有獨立執行緒，掃描最新直播項目，偵測 `is_live` 時直接排入直播錄製佇列；不等待直播結束。
- **頻道外觀**：從 yt-dlp 的 YouTube 頻道 metadata 取得真正的頭像與 banner，快取在 `data/assets/`。新增頻道完成首次掃描後顯示；重新掃描會保留新版本，不改變已選擇的圖片。頻道內「重新取得 YouTube 圖片」會取得並套用最新圖片；「更換頭像 / Banner」可分別選回任一舊版本。相同圖片依 SHA-256 去重。v2 舊圖片會在升級時納入歷史。圖片不可取得時顯示漸層背景／名字首字，不會借用其他頻道圖片。
- **點進頻道**：YouTube 風格的頻道 banner、頭像及縮圖影音庫，分成「影片 / Shorts」與「直播 / 回放」。預設「已下載」顯示已完成原檔的收藏；切換「全部」查看排程和直播錄製狀態。
- **網頁播放**：點已下載縮圖／標題開啟播放器，可選影片播放副本或 MP3。伺服器支援 HTTP Range，方便瀏覽器分段讀取／拖曳進度。
- **網頁下載**：卡片提供「最高畫質原檔」與「MP3」按鈕；播放器另可下載播放副本。
- **設定頁**：YouTube、Twitch、LGBTQ+ / Trans、Dark mode、Light mode、Neon、Neon pink 七種主題；可微調主色、背景與卡片色。先預覽再按儲存。設定保存於 SQLite，供此影音庫帳號共用；預設 Trans 粉藍／粉紅／白色。
- **單一連結下載**：YouTube 影片或直播網址、X.com / Twitter 貼文網址；可選擇獨立下載或存入現有訂閱影音庫。多影片貼文由 yt-dlp extractor 處理。
- **全部任務**：查看所有頻道與單次下載的任務，每頁 100 項。
- **Log 紀錄**：最近 300 行，每 5 秒刷新，可依任務 ID 篩選。每日更新流程將資料庫 Log 保留最近 100,000 行；Docker 輸出另設輪替。
- **失敗重試**：自動退避重試，連續 5 次失敗後停止；Web UI 有重試按鈕。
- **暫停追蹤**：停止該頻道的新檢查和新任務啟動。已開始的錄製／下載繼續，已下載的檔案和紀錄保留。
- **重啟復原**：下載中／錄製中的任務會重新排程，使用 yt-dlp 暫存檔及每任務 archive 避免重新下載已完成項目。直播中斷後可能缺片段，不保證無縫接續。
- **即時更新按鈕**：Log 頁可手動觸發 yt-dlp 更新檢查。

## 多來源共用訂閱

在頻道內展開「來源管理與訂閱名稱」，加入 YouTube、Twitch 或 X 帳號網址。同一位創作者可以有多個 YouTube 頻道或跨平台帳號，仍顯示成一張訂閱卡片。下載內容以來源標記，但共用影片／直播分頁、播放器及下載功能。可單獨暫停每個來源或暫停整個訂閱；已開始的任務繼續完成，檔案與紀錄不刪除。

- YouTube：可列出的公開／有權限的影片、Shorts、回放，以及開播時錄製。
- Twitch：可取得的 VOD 與 Clips；頻道開播時立即排程邊播邊錄製。直播會從偵測當下開始，缺失片段與回放保存範圍受來源限制。
- Twitter / X：單篇影片／GIF／直播貼文由 yt-dlp 下載。帳號影片貼文清單透過官方 X API v2 user posts endpoint 分頁追蹤，需你提供有相應 API 權限的 Bearer Token；X API 的歷史範圍、限速與額度依帳號存取方案，不保證抓到整個帳號全部歷史。沒有 Token 時仍可下載單篇網址並手動歸入同一影音庫。
- 設定好 X API Token 後，新貼文每 5 分鐘檢查一次；全量歷史仍按 SCAN_SECONDS 排程。官方 API 可讀取且包含影片、GIF 或 broadcast 連結的貼文才會加入佇列。

## Web UI 設定

「設定與配色」提供配色、各平台 cookies、官方 X API Token，以及下表全部執行設定。僅 WEB_PASSWORD、WEB_USER、WEB_PORT、BIND_IP 留在 .env；Web UI 不提供這四項修改，也不接受用 API 修改它們。

SQLite 內的已保存設定優先於 .env 預設，重啟後保留。周期變更會喚醒排程；SCAN_SECONDS 變更會重新安排來源掃描。工作數可直接增減，新任務使用新的工作數；減少並行不停止已啟動任務。畫質、轉檔和 cookies 設定套用到新程序，進行中的程序使用自己的設定／cookies 快照。

## 預設設定

前四項帳號／監聽設定在 `.env`，修改後執行 `docker compose up -d`。其餘可直接在 Web UI 儲存。

| 變數 | 預設 | 用途 |
|---|---:|---|
| WEB_PORT | 8088 | 主機 Web UI port |
| BIND_IP | 0.0.0.0 | 主機綁定 IP；僅本機可改成 127.0.0.1 |
| WEB_USER | admin | 登入帳號 |
| WEB_PASSWORD | 必填 | 登入密碼；請修改範例值 |
| LIVE_POLL_SECONDS | 30 | YouTube / Twitch 直播檢查週期，秒 |
| LIVE_SCAN_LIMIT | 10 | YouTube 最新直播項目檢查數 |
| TWITTER_POLL_SECONDS | 300 | X 新貼文檢查週期，秒 |
| SCAN_SECONDS | 21600 | 歷史及新影片全量掃描週期，秒 |
| UPDATE_SECONDS | 86400 | yt-dlp 自動更新檢查週期，秒 |
| DOWNLOAD_WORKERS | 2 | 一般影片／回放下載並行數 |
| LIVE_WORKERS | 4 | 直播錄製並行數，與一般下載分開 |
| TRANSCODE_WORKERS | 1 | MP3／網頁播放副本轉檔並行數 |
| TRANSCODE_THREADS | 2 | 每個播放副本轉檔的視訊執行緒數 |
| PLAYBACK_HEIGHT | 1080 | 播放副本最大高度；最高畫質原檔不受限制 |
| YTDLP_CHANNEL | stable | stable；nightly 會使用 PyPI 的 prerelease |
| LIVE_FROM_START | false | true：嘗試從開播起點錄製；false：從偵測當下開始 |

直播檢查預設每頻道檢查 `/streams` 最新 10 項，可在 Web UI 調整 LIVE_SCAN_LIMIT 擴大。頻道較多或網站回應慢時，實際一輪檢查可能超過 30 秒。超過 4 個同時直播會等直播 worker 空出；需要更多並行時提高 LIVE_WORKERS。歷史掃描獨立進行，因此大型頻道的首次全量掃描不會阻擋直播檢查。

原檔使用 `bv*+ba/b` 及強制 `res,fps,vbr,abr` 排序，優先選最高可取得解析度、影格率與位元率，不設定 1080p／4K 上限。需要合併時使用 MKV，原檔不重新編碼。來源為單一檔案時可能保留 MP4 / WebM 等來源格式。錄製期間 `.part` 或音畫暫存檔持續寫入，完整合併後才在 Web UI 提供下載。除了影音，也保存 thumbnail、info JSON、完成檔案 manifest 和 archive。

原檔完成後，獨立轉檔佇列從已下載原檔製作：
- `.audio.mp3`：FFmpeg libmp3lame VBR 品質 0 留存副本，保留原始影音不刪除；MP3 是有損格式。
- `.playback.mp4`：H.264 / AAC、yuv420p、faststart 的瀏覽器相容副本，預設最高 1080p，較低解析度來源不放大；最高畫質原檔仍完整留存，下載按鈕指向原檔。HDR 原檔保留 HDR，本版播放副本未加入 HDR→SDR tone mapping。

轉檔完成前原檔仍可下載，Web UI 會顯示副本製作中；轉檔失敗可單獨重試，不必重抓影片。沒有音軌的影片會跳過 MP3 並在 Log 說明。直播須等結束並完成原檔合併後才製作副本。每部影片多存兩份副本會增加磁碟及 CPU 使用量。

## 儲存位置 / NAS 掛載

`./data` 保存 SQLite 資料庫和 yt-dlp 更新版本；`./downloads` 保存影音；`./cookies` 保存選用 cookies。

下載路徑例如：

```text
downloads/channel-1/影片/任務ID/日期_標題_[影片ID].mkv
downloads/channel-1/直播/任務ID/日期_標題_[影片ID].mkv
downloads/single/影片/任務ID/…
```

要下載到 NAS 資料夾，修改 compose volumes：

```yaml
volumes:
  - ./data:/data
  - /volume1/video/channel-vault:/downloads
  - ./cookies:/cookies
```

Windows host path 可用 `D:/Videos/channel-vault:/downloads`。保留 `data` 和 `downloads` 才能保留歷史與續傳狀態。停止容器後再備份這兩個目錄；不同電腦搬移時維持相同容器內 `/data`、`/downloads` 路徑。

更新留下的舊 Python runtime 會保留，以免影響使用舊版本的錄製。若累積太多，可在停止容器後清掉 `data/runtimes/` 中除 `data/runtime-current` 所指向目錄之外的舊目錄。

## Cookies 與網站限制

Web UI 支援按平台上傳 `.txt` 或貼上 Netscape cookies 內容、啟用／停用 cookies，以及移除專用 cookies。檔案最多 2 MB；會驗證 7 個 Tab 分隔欄位。YouTube 會員影片需要登入帳號原本就有會員觀看權限，cookies 不會繞過權限。

登入檔保存在 `cookies/youtube.txt`、`cookies/twitch.txt`、`cookies/twitter.txt`；X API Bearer Token 保存在 `cookies/x-bearer.txt`。API 僅回傳是否設定，不回傳任何 cookie 或 token 內容；前端也不保存秘密值。每個 yt-dlp 程序各自使用臨時快照，避免並行回寫共享 cookies。快照位於 data/cookie-runs，完成後刪除；非正常終止可能留下快照，備份時也須視為登入憑證。

若沒有平台專用檔案，仍向下相容使用 `cookies/cookies.txt`。移除平台專用檔案會回退到舊檔案；要完全不使用 cookies，可在該平台關閉啟用開關。


若 YouTube / X 要求登入，將自己匯出的 Netscape 格式 cookies 放在：

```text
cookies/cookies.txt
```

新任務自動讀取，不需重新 build。Web UI 需保存登入資料，所以掛載 cookies 目錄是可寫的。Cookies 屬於登入憑證，請勿公開或提交 git。

登入、地區、年齡、會員權限、IP 限速、YouTube PO Token 或 extractor 變更仍可能造成失敗。Cookies 不保證解決所有問題；本版沒有 PO Token provider。私人／刪除／沒有觀看權限／未公開回放的內容無法保證取得。「所有影片」指頻道頁可列出且此環境有權限取得的影片。

直播從偵測當下開始，可能漏掉前面的秒數。`LIVE_FROM_START=true` 使用 yt-dlp 的實驗功能，能否補回開頭受 DVR 和來源格式限制。直播結束後才合併影音，這不代表等結束才下載；可直接觀察 `downloads` 裡錄製中的檔案大小。

Web UI 預設 HTTP，Basic Auth 在 HTTP 上沒有傳輸加密；供內網使用。若需要從外網使用，放在自己的 HTTPS reverse proxy / Cloudflare Tunnel 後方。

## 從 v1 / v2 升級

停止舊容器，備份 `data` 和 `downloads`，以本版替換 `app.py`、`providers.py`、`web/`、Dockerfile 與 compose.yaml；保留自己的 `.env`、cookies、資料庫和下載檔。執行 `docker compose up -d --build`。

資料庫會自動新增所需欄位、來源列表與圖片歷史並保留原紀錄；舊訂閱自動轉成一個 YouTube 來源。既有完成的原檔會排程補做 MP3／播放副本。圖片會在下一次頻道掃描取得，也可手動點重新掃描。不要用 ZIP 裡空目錄覆蓋或清空既有資料。

## 維護

```bash
# 查看服務狀態
docker compose ps
# 查看容器輸出
docker compose logs -f --tail=100
# 套用設定變更
docker compose up -d
# 修改本專案程式碼或更新基底元件後重建
docker compose build --pull
docker compose up -d
# 停止（資料保留）
docker compose down
```

開發測試：

```bash
python -m unittest -v
node --check web/app.js
```

已通過 24 項後端測試，包括資料持久化、去重、直播佇列優先、一般任務改派直播 worker、重啟復原、失敗重試、路徑限制、HTTP API 認證、主題設定保存、頻道圖片快取、轉檔重啟復原、資料庫升級保留紀錄、圖片歷史保存與選回、設定排除認證／監聽欄位、cookies 只寫與獨立快照、多來源共用影音庫、Twitch 開播偵測與回放發現、X API 影片篩選／分頁，以及檔案 Range／HEAD／下載。

使用真實 FFmpeg 製作短影音並轉成 MP3 與 H.264/AAC 播放副本，驗證原檔 SHA-256 不變。各平台下載、直播與官方 X API 使用模擬測試驗證參數和狀態；產生套件的環境沒有 Docker 或可用瀏覽器，因此尚未實際 build、進行瀏覽器播放實測，也未連線 YouTube / X 進行端到端下載或長時間錄製。

官方參考：
- https://github.com/yt-dlp/yt-dlp
- https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
- https://docs.deno.com/runtime/reference/docker/

## 專案工作方式

每次完成修改後會提供與當前實作一致的完成頁面預覽。預覽使用示範資料，與真實來源的端到端實測分開標示。此偏好已寫入 AGENTS.md。

Git 不追蹤空目錄，ZIP 發行檔另外寫入空的 data/、downloads/、cookies/；版本控制不包含任何資料庫、cookies、token、.env 或下載媒體。


## V4：免費 X 追蹤與選用 Token
預設 `X_DISCOVERY_MODE=cookies`，在設定頁上傳並啟用 Netscape 格式的 Twitter / X 登入 cookies，gallery-dl 掃描 `/media` 後將影片貼文交给 yt-dlp。保留多來源訂閱、原檔、MP3 與播放副本。設定頁可以明確選擇 `api` 使用已保存的官方 Bearer Token；不會自動退回付費 API。既有 Token 保留但預設不使用。既有任務使用相同 twitter media key 去重。

歷史掃描最多執行 30 分鐘，定期輪詢最近 100 則媒體貼文最多 5 分鐘，超出範圍可能漏抓，請使用重新掃描補查。免費模式僅探索帳號 media 影片/GIF，不保證 X 直播即時發現或完整歷史。cookies 過期、限流與網站改版可能使掃描失效。gallery-dl 與 yt-dlp 一起自動更新。切換模式會重新掃描；已有下載保留。訂閱或來源暫停時不下載佇列。

Docker 對外固定 8088、容器內 8080。升級時保留 data/downloads/cookies 與 .env，重建映像（只重啟不會更新程式）：`docker compose up -d --build`。
