# 築日常｜自動發布器

Instagram 與 Threads 的定時發布。

內容由 Claude 每週產出，成品與 `manifest.json` 放在 Google Drive；
這裡的 GitHub Actions 每 15 分鐘去 Drive 檢查一次，到期且已核准就發出去。

Meta 的 access token 存在 GitHub Secrets，**不會進到 Claude**。

---

## 為什麼是這個架構

兩道實測出來的限制：

1. Claude 的執行環境**連不到** `graph.instagram.com` / `graph.threads.net`（proxy 回 403），所以 Claude 自己發不了文。
2. Claude 的執行環境**不能寫入任何 GitHub repo**（git proxy 擋下非授權 repo 的推送）。

但 Claude **可以寫 Google Drive**，GitHub Actions **可以寫自己的 repo**。
所以用 Drive 當交接站：

```
Claude   → 成品圖片與影片 + manifest.json 寫進 Google Drive（公開可讀）
            ↓
Actions  → 每 15 分鐘讀 manifest
         → 到期 + approved 才動作
         → 從 Drive 下載媒體，commit 進本 repo（產生 raw.githubusercontent 公開網址）
         → 確認圖片網址真的抓得到，才呼叫 Meta API
         → 結果寫進 published/log.jsonl
```

`published/log.jsonl` 是這套系統的記憶：發過的任務 id 不會再發第二次。

---

## 一次性設定

### 1. repo 必須是 public

Meta 的伺服器要自己去抓 `raw.githubusercontent.com` 上的圖。
repo 裡沒有任何金鑰，token 全部在 Secrets，public 不影響安全。

### 2. Variables

Settings → Secrets and variables → Actions → **Variables**

| 名稱 | 值 |
|---|---|
| `MEDIA_BASE_URL` | `https://raw.githubusercontent.com/<帳號>/<repo>/main/` |
| `MANIFEST_FILE_ID` | Drive 上 `manifest.json` 的檔案 ID |

`manifest.json` 必須設成「**知道連結的任何人都可以檢視**」，
而且**永遠是同一個檔案**（每週覆寫內容，不要重建，否則 ID 會變）。

### 3. Secrets

Settings → Secrets and variables → Actions → **Secrets**

| 名稱 | 說明 |
|---|---|
| `IG_USER_ID` | Instagram 專業帳號的 user id |
| `IG_TOKEN` | Instagram 長效 access token（60 天，會自動續） |
| `TH_USER_ID` | Threads 帳號的 user id |
| `TH_TOKEN` | Threads 長效 access token |
| `GH_PAT` | 只給這個 repo、Secrets 為 Read and write 的 fine-grained token，供自動續 token 用 |

### 4. 測試

Actions → 築日常自動發布 → Run workflow → 勾**只驗證不發文** → Run。
會印出將要發的圖片網址與字數，但不會真的送出。

---

## manifest.json 格式

```json
{
  "generated_at": "2026-09-14T21:00:00+08:00",
  "jobs": [
    {
      "id": "2026-09-15T0800_post",
      "publish_at": "2026-09-15T08:00:00+08:00",
      "approved": false,
      "max_late_hours": 6,
      "platforms": ["instagram", "threads"],
      "instagram": {
        "type": "carousel",
        "images": [
          {"name": "01.jpg", "drive_id": "1AbC..."},
          {"name": "02.jpg", "drive_id": "1DeF..."}
        ],
        "caption": "IG 內文，最多 2200 字元"
      },
      "threads": {
        "type": "text",
        "text": "Threads 內文，最多 500 字元"
      }
    },
    {
      "id": "2026-09-15T2030_reels",
      "publish_at": "2026-09-15T20:30:00+08:00",
      "approved": false,
      "platforms": ["instagram"],
      "instagram": {
        "type": "reels",
        "video": {"name": "reels.mp4", "drive_id": "1GhI..."},
        "caption": "Reels 內文"
      }
    }
  ]
}
```

- `publish_at` 用 ISO 8601 含時區，沒寫時區就當作 `+08:00`。
- **`approved` 是 `false` 的任務永遠不會被發出**，就算時間到了也一樣。
- Drive 上的每個媒體檔也都要設成「知道連結的任何人都可以檢視」。

---

## 兩道保護

**沒核准不發。** `approved` 必須是 `true`。

**過期不發。** 任務逾時超過 `max_late_hours`（預設 6 小時）未發出，
會被記成 `stale` 並永久跳過。避免兩天後突然冒出一則過期內容。
要重發就改 `publish_at` 與 `id` 再核准。

---

## 平台限制（publish.py 會擋）

| 項目 | 限制 |
|---|---|
| IG 輪播張數 | 2–10 張 |
| IG caption | 2200 字元 |
| IG 圖片格式 | **只吃 JPEG**，且必須是公開網址 |
| IG Reels | MP4，建議 1080×1920 |
| IG 發文額度 | 24 小時 100 則（輪播算 1 則） |
| Threads 內文 | **500 字元**（含空白換行） |
| Threads 圖片 | JPEG / PNG，最大 8 MB，寬 320–1440 px |
| Threads 發文額度 | 24 小時 250 則 |

透過 API 發 Reels **無法套用 Instagram 的熱門音樂**，只能用影片本身內含的音軌。

---

## 出錯時

失敗會寫進 `published/log.jsonl`，狀態為 `failed`，下一輪會重試。
`published/log.jsonl` 裡狀態為 `published` / `stale` 的 id 不會再被處理。

常見原因：

- **Drive 檔案回傳網頁** — 該檔案沒設成「知道連結的任何人都可以檢視」。
- **圖片網址抓不到** — 確認 repo 是 public、`MEDIA_BASE_URL` 結尾有 `/`。
- **token 過期** — 手動跑一次「更新 Meta 長效 token」。超過 60 天沒續就得重新授權。
- **IG 說格式不對** — 圖必須是真的 JPEG，不是 PNG 改副檔名。
