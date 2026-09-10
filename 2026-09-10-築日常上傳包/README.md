# 築日常｜自動發布器

Instagram 與 Threads 的定時發布。內容由 Claude 每日產出並推進 `queue/`，
這裡的 GitHub Actions 負責在指定時間把它發出去。

Meta 的 access token 存在 GitHub Secrets，**不會進到 Claude**。

---

## 這個 repo 怎麼運作

```
queue/2026-09-11T0800_A.json   ← Claude 每天推進來的發文任務（approved: false）
        │
        │  人工核准 → approved: true
        ▼
.github/workflows/publish.yml  ← 每 15 分鐘檢查一次
        │  到期 + 已核准
        ▼
publish.py  →  Instagram 輪播 + Threads 貼文
        │
        ▼
published/2026-09-11T0800_A.json  +  published/log.jsonl
```

圖片放在 `media/`，發文時 Meta 會用 `MEDIA_BASE_URL` 直接抓圖，
所以**這個 repo 必須是 public**（Meta 的伺服器要抓得到圖）。
repo 裡沒有任何金鑰，token 全部在 Secrets，public 不影響安全。

---

## 一次性設定

### 1. 建立 repo

建一個 **public** repo（例如 `zhuriichang-publisher`），把這些檔案放進去。

### 2. 設定變數

Settings → Secrets and variables → Actions

**Variables**（不敏感）

| 名稱 | 值 |
|---|---|
| `MEDIA_BASE_URL` | `https://raw.githubusercontent.com/<你的帳號>/<repo>/main/` |

**Secrets**（敏感）

| 名稱 | 說明 |
|---|---|
| `IG_USER_ID` | Instagram 專業帳號的 user id |
| `IG_TOKEN` | Instagram 長效 access token（60 天，會自動續） |
| `TH_USER_ID` | Threads 帳號的 user id |
| `TH_TOKEN` | Threads 長效 access token（60 天，會自動續） |
| `GH_PAT` | 只給這個 repo、Secrets 為 Read and write 的 fine-grained token，供自動續 token 用 |

取得 Meta 那四個值的步驟見另一份設定指南。

### 3. 測試

Actions → 築日常自動發布 → Run workflow，把 **只驗證不發文** 打勾。
會印出將要發的圖片網址與字數，但不會真的發出去。

---

## 每天的流程

1. 台北時間 08:00，Claude 產出草稿、算好圖卡、推進 `queue/`（`approved: false`）。
2. 你在審核頁按核准。
3. Claude 隔日 07:30 把 `approved` 改成 `true`（或你自己在 GitHub 上改）。
4. 08:00 A 發出、20:30 B 發出。

`approved` 是 `false` 的任務**永遠不會被發出**，就算時間到了也一樣。

反過來也有保護：任務逾時超過 **6 小時**未發出，會被標成 `status: "stale"` 並跳過。財經早報過期就沒有意義，不會在兩天後才突然冒出來。要重發就更新 `publish_at` 再核准。單篇可用 `max_late_hours` 調整這個時數。

---

## 發文任務格式

```json
{
  "id": "2026-09-11T0800_A",
  "series": "A",
  "publish_at": "2026-09-11T08:00:00+08:00",
  "approved": false,
  "platforms": ["instagram", "threads"],
  "instagram": {
    "type": "carousel",
    "images": ["media/A-20260910/A-20260910-01.jpg", "..."],
    "caption": "IG 內文，最多 2200 字元"
  },
  "threads": {
    "type": "text",
    "text": "Threads 內文，最多 500 字元"
  }
}
```

`publish_at` 用 ISO 8601 含時區，沒寫時區就當作 `+08:00`。

---

## 平台限制（publish.py 會擋）

| 項目 | 限制 |
|---|---|
| IG 輪播張數 | 2–10 張 |
| IG caption | 2200 字元 |
| IG 圖片格式 | **只吃 JPEG**，且必須是公開網址 |
| IG 發文額度 | 24 小時 100 則（輪播算 1 則） |
| Threads 內文 | **500 字元**（比築日常規範的 550 字更嚴，以這個為準） |
| Threads 圖片 | JPEG / PNG，最大 8 MB，寬 320–1440 px |
| Threads 發文額度 | 24 小時 250 則 |

---

## 出錯時

失敗的任務會留在 `queue/`，並寫入 `status: "failed"`、`error`、`attempts`。
連續失敗 3 次後就不再重試，等人工處理。`published/log.jsonl` 有完整紀錄。

常見原因：

- **token 過期** — 手動跑一次「更新 Meta 長效 token」workflow。超過 60 天沒續就得重新授權。
- **圖片抓不到** — 確認 repo 是 public、`MEDIA_BASE_URL` 結尾有 `/`、檔案真的在 `media/`。
- **IG 說格式不對** — 圖必須是 JPEG，不是 PNG 改副檔名。
