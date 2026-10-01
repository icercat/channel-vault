# Channel Vault — 頻道自動下載 / 即時直播錄製

自訂、可自行架設的 Docker Compose 專案。包含後端、繁體中文 Web UI、SQLite 持久化任務佇列和 yt-dlp 自動更新，無須另外架資料庫。

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

- **訂閱頻道**：輸入 `https://www.youtube.com/@帳號` 或 `/channel/UC…`。
- 新訂閱立即安排掃描 `/videos`、`/shorts`、`/streams` 的全部可取得項目，然後每 6 小時重新掃描。每個頻道的影片 ID 去重。
- 直播監控另有獨立執行緒，掃描最新直播項目，偵測 `is_live` 時直接排入直播錄製佇列；不等待直播結束。
- **點進頻道**：分成「影片 / Shorts」與「直播 / 直播回放」，可查看任務狀態、下載檔案與個別 Log。
- **單一連結下載**：YouTube 影片或直播網址、X.com / Twitter 貼文網址；不提供 X 帳號訂閱。多影片貼文由 yt-dlp extractor 處理。
- **全部任務**：查看所有頻道與單次下載的任務，每頁 100 項。
- **Log 紀錄**：最近 300 行，每 5 秒刷新，可依任務 ID 篩選。每日更新流程將資料庫 Log 保留最近 100,000 行；Docker 輸出另設輪替。
- **失敗重試**：自動退避重試，連續 5 次失敗後停止；Web UI 有重試按鈕。
- **暫停追蹤**：停止該頻道的新檢查和新任務啟動。已開始的錄製／下載繼續，已下載的檔案和紀錄保留。
- **重啟復原**：下載中／錄製中的任務會重新排程，使用 yt-dlp 暫存檔及每任務 archive 避免重新下載已完成項目。直播中斷後可能缺片段，不保證無縫接續。
- **即時更新按鈕**：Log 頁可手動觸發 yt-dlp 更新檢查。

## 預設設定

設定在 `.env`，修改後執行 `docker compose up -d`。

| 變數 | 預設 | 用途 |
|---|---:|---|
| WEB_PORT | 8088 | 主機 Web UI port |
| BIND_IP | 0.0.0.0 | 主機綁定 IP；僅本機可改成 127.0.0.1 |
| WEB_USER | admin | 登入帳號 |
| WEB_PASSWORD | 必填 | 登入密碼；請修改範例值 |
| LIVE_POLL_SECONDS | 30 | 直播檢查週期，秒 |
| SCAN_SECONDS | 21600 | 歷史及新影片全量掃描週期，秒 |
| UPDATE_SECONDS | 86400 | yt-dlp 自動更新檢查週期，秒 |
| DOWNLOAD_WORKERS | 2 | 一般影片／回放下載並行數 |
| LIVE_WORKERS | 4 | 直播錄製並行數，與一般下載分開 |
| YTDLP_CHANNEL | stable | stable；nightly 會使用 PyPI 的 prerelease |
| LIVE_FROM_START | false | true：嘗試從開播起點錄製；false：從偵測當下開始 |

直播檢查預設每頻道檢查 `/streams` 最新 10 項，可在 compose 的 `environment` 加 `LIVE_SCAN_LIMIT: 50` 擴大。頻道較多或網站回應慢時，實際一輪檢查可能超過 30 秒。超過 4 個同時直播會等直播 worker 空出；需要更多並行時提高 LIVE_WORKERS。歷史掃描獨立進行，因此大型頻道的首次全量掃描不會阻擋直播檢查。

檔案使用最高可取得畫質 `bv*+ba/b`，需要合併時使用 MKV，不重新編碼。來源為單一檔案時可能保留 MP4 / WebM 等來源格式。錄製期間 `.part` 或音畫暫存檔持續寫入，完整合併後才在 Web UI 提供下載。除了影音，也保存 thumbnail、info JSON、完成檔案 manifest 和 archive。

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

若 YouTube / X 要求登入，將自己匯出的 Netscape 格式 cookies 放在：

```text
cookies/cookies.txt
```

新任務自動讀取，不需重新 build。yt-dlp 可能回寫 cookies，所以掛載 cookies 目錄是可寫的。Cookies 屬於登入憑證，請勿公開或提交 git。

登入、地區、年齡、會員權限、IP 限速、YouTube PO Token 或 extractor 變更仍可能造成失敗。Cookies 不保證解決所有問題；本版沒有 PO Token provider。私人／刪除／沒有觀看權限／未公開回放的內容無法保證取得。「所有影片」指頻道頁可列出且此環境有權限取得的影片。

直播從偵測當下開始，可能漏掉前面的秒數。`LIVE_FROM_START=true` 使用 yt-dlp 的實驗功能，能否補回開頭受 DVR 和來源格式限制。直播結束後才合併影音，這不代表等結束才下載；可直接观察 `downloads` 裡錄製中的檔案大小。

Web UI 預設 HTTP，Basic Auth 在 HTTP 上沒有傳輸加密；供內網使用。若需要從外網使用，放在自己的 HTTPS reverse proxy / Cloudflare Tunnel 後方。

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

已通過 12 項測試，包括資料持久化、去重、直播佇列優先、一般任務改派直播 worker、重啟復原、失敗重試、路徑限制及 HTTP API 認證。下載與直播程序使用模擬測試驗證參數和狀態；產生套件的環境沒有 Docker，因此尚未實際 build，也未連線 YouTube / X 進行端到端下載或長時間錄製。

官方參考：
- https://github.com/yt-dlp/yt-dlp
- https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
- https://docs.deno.com/runtime/reference/docker/
