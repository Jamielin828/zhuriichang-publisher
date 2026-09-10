#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
築日常｜Instagram + Threads 自動發布器

由 GitHub Actions 定時執行。掃描 queue/ 底下到期的發文任務，
依序發到 Instagram 與 Threads，成功後移到 published/。

只用標準函式庫，不需要 pip install。

需要的環境變數（存在 GitHub Secrets）：
  IG_USER_ID       Instagram 專業帳號的 user id
  IG_TOKEN         Instagram 長效 access token
  TH_USER_ID       Threads 帳號的 user id
  TH_TOKEN         Threads 長效 access token
  MEDIA_BASE_URL   圖片的公開網址前綴，例如
                   https://raw.githubusercontent.com/<user>/<repo>/main/
可選：
  DRY_RUN=1        只驗證不發文
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

IG_HOST = "https://graph.instagram.com"
IG_VER = os.environ.get("IG_API_VERSION", "v23.0")
TH_HOST = "https://graph.threads.net"
TH_VER = os.environ.get("TH_API_VERSION", "v1.0")

TPE = timezone(timedelta(hours=8))
QUEUE_DIR = "queue"
PUBLISHED_DIR = "published"
LOG_PATH = "published/log.jsonl"

IG_CAPTION_MAX = 2200
TH_TEXT_MAX = 500          # Threads 單則上限 500 字元
IG_CAROUSEL_MAX = 10
MAX_LATE_HOURS = 6          # 逾時超過這個小時數就不發，避免發出過期的財經內容
DRY = os.environ.get("DRY_RUN") == "1"


class PublishError(Exception):
    pass


# ---------------------------------------------------------------- http
def _call(url, params, method="POST"):
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(url + "?" + data.decode(), method="GET")
        body = None
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error", {})
            msg = "%s (code %s, subcode %s)" % (
                err.get("message", raw), err.get("code"), err.get("error_subcode"))
        except Exception:
            msg = raw
        raise PublishError("HTTP %s from %s — %s" % (e.code, url, msg))
    except urllib.error.URLError as e:
        raise PublishError("network error calling %s — %s" % (url, e))


def ig(path, params, method="POST"):
    p = dict(params)
    p["access_token"] = os.environ["IG_TOKEN"]
    return _call("%s/%s/%s" % (IG_HOST, IG_VER, path.lstrip("/")), p, method)


def th(path, params, method="POST"):
    p = dict(params)
    p["access_token"] = os.environ["TH_TOKEN"]
    return _call("%s/%s/%s" % (TH_HOST, TH_VER, path.lstrip("/")), p, method)


def media_url(rel):
    base = os.environ["MEDIA_BASE_URL"].rstrip("/") + "/"
    return base + rel.lstrip("/")


# ---------------------------------------------------------------- instagram
def ig_wait(container_id, label, tries=30, delay=6):
    """輪詢容器狀態直到 FINISHED。"""
    for i in range(tries):
        r = ig(container_id, {"fields": "status_code,status"}, method="GET")
        code = r.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise PublishError("IG 容器 %s (%s) 處理失敗：%s"
                               % (container_id, label, r.get("status")))
        time.sleep(delay)
    raise PublishError("IG 容器 %s (%s) 逾時未就緒" % (container_id, label))


def publish_instagram(job):
    spec = job["instagram"]
    uid = os.environ["IG_USER_ID"]
    caption = spec["caption"]
    if len(caption) > IG_CAPTION_MAX:
        raise PublishError("IG caption 超過 %d 字元（目前 %d）"
                           % (IG_CAPTION_MAX, len(caption)))
    images = spec.get("images", [])
    if not images:
        raise PublishError("IG 任務沒有任何圖片")
    if len(images) > IG_CAROUSEL_MAX:
        raise PublishError("IG 輪播最多 %d 張（目前 %d）"
                           % (IG_CAROUSEL_MAX, len(images)))

    if DRY:
        print("  [DRY] IG 會發 %d 張輪播，caption %d 字元" % (len(images), len(caption)))
        for rel in images:
            print("        " + media_url(rel))
        return {"dry_run": True}

    # 額度檢查
    try:
        lim = ig("%s/content_publishing_limit" % uid,
                 {"fields": "config,quota_usage"}, method="GET")
        usage = (lim.get("data") or [{}])[0]
        print("  IG 24 小時額度用量：%s" % usage.get("quota_usage"))
    except PublishError as e:
        print("  （額度查詢失敗，繼續：%s）" % e)

    if len(images) == 1:
        cid = ig("%s/media" % uid,
                 {"image_url": media_url(images[0]), "caption": caption})["id"]
        ig_wait(cid, "single")
        res = ig("%s/media_publish" % uid, {"creation_id": cid})
    else:
        children = []
        for n, rel in enumerate(images, 1):
            c = ig("%s/media" % uid,
                   {"image_url": media_url(rel), "is_carousel_item": "true"})["id"]
            print("  IG 子容器 %d/%d → %s" % (n, len(images), c))
            children.append(c)
        for n, c in enumerate(children, 1):
            ig_wait(c, "item %d" % n)
        parent = ig("%s/media" % uid,
                    {"media_type": "CAROUSEL",
                     "children": ",".join(children),
                     "caption": caption})["id"]
        ig_wait(parent, "carousel")
        res = ig("%s/media_publish" % uid, {"creation_id": parent})
    print("  IG 已發布：%s" % res.get("id"))
    return res


# ---------------------------------------------------------------- threads
def th_wait(container_id, tries=20, delay=5):
    for i in range(tries):
        r = th(container_id, {"fields": "status,error_message"}, method="GET")
        st = r.get("status")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise PublishError("Threads 容器 %s 失敗：%s"
                               % (container_id, r.get("error_message")))
        time.sleep(delay)
    raise PublishError("Threads 容器 %s 逾時未就緒" % container_id)


def publish_threads(job):
    spec = job["threads"]
    uid = os.environ["TH_USER_ID"]
    text = spec["text"]
    if len(text) > TH_TEXT_MAX:
        raise PublishError("Threads 內文超過 %d 字元（目前 %d）"
                           % (TH_TEXT_MAX, len(text)))
    images = spec.get("images", [])

    if DRY:
        print("  [DRY] Threads 會發 %d 字元，%d 張圖" % (len(text), len(images)))
        return {"dry_run": True}

    if not images:
        cid = th("%s/threads" % uid, {"media_type": "TEXT", "text": text})["id"]
    elif len(images) == 1:
        cid = th("%s/threads" % uid,
                 {"media_type": "IMAGE", "image_url": media_url(images[0]),
                  "text": text})["id"]
    else:
        kids = []
        for rel in images[:20]:
            k = th("%s/threads" % uid,
                   {"media_type": "IMAGE", "image_url": media_url(rel),
                    "is_carousel_item": "true"})["id"]
            kids.append(k)
        cid = th("%s/threads" % uid,
                 {"media_type": "CAROUSEL", "children": ",".join(kids),
                  "text": text})["id"]
    # 官方建議發布前等待約 30 秒
    time.sleep(30)
    try:
        th_wait(cid)
    except PublishError as e:
        print("  （狀態查詢略過：%s）" % e)
    res = th("%s/threads_publish" % uid, {"creation_id": cid})
    print("  Threads 已發布：%s" % res.get("id"))
    return res


# ---------------------------------------------------------------- queue
def load_jobs():
    if not os.path.isdir(QUEUE_DIR):
        return []
    out = []
    for name in sorted(os.listdir(QUEUE_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(QUEUE_DIR, name)
        with open(path, encoding="utf-8") as f:
            job = json.load(f)
        job["_path"] = path
        out.append(job)
    return out


def when_of(job):
    at = job.get("publish_at")
    if not at:
        raise PublishError("缺少 publish_at")
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        raise PublishError("publish_at 格式錯誤：%r（需 ISO 8601，例如 2026-09-11T08:00:00+08:00）" % at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=TPE)
    return when


def is_due(job, now):
    return when_of(job) <= now


def is_stale(job, now):
    """財經早報過期就沒有意義。逾時太久一律不發，改由人工處理。"""
    limit = job.get("max_late_hours", MAX_LATE_HOURS)
    return now - when_of(job) > timedelta(hours=limit)


def log(entry):
    os.makedirs(PUBLISHED_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def save(job):
    path = job.pop("_path")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    job["_path"] = path


def archive(job):
    os.makedirs(PUBLISHED_DIR, exist_ok=True)
    src = job["_path"]
    dst = os.path.join(PUBLISHED_DIR, os.path.basename(src))
    body = dict(job)
    body.pop("_path", None)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    os.remove(src)


NEEDED_ENV = {
    "instagram": ("IG_USER_ID", "IG_TOKEN"),
    "threads": ("TH_USER_ID", "TH_TOKEN"),
}


def missing_env(platform):
    """回傳該平台缺少的環境變數，沒缺就是空 list。"""
    return [k for k in NEEDED_ENV.get(platform, ()) if not os.environ.get(k)]


def main():
    if not os.environ.get("MEDIA_BASE_URL"):
        print("缺少環境變數 MEDIA_BASE_URL", file=sys.stderr)
        return 2

    now = datetime.now(TPE)
    print("現在時間（台北）：%s" % now.isoformat(timespec="seconds"))
    jobs = load_jobs()
    if not jobs:
        print("queue 是空的，沒有要發的內容。")
        return 0

    did = 0
    failed = 0
    for job in jobs:
        jid = job.get("id", os.path.basename(job["_path"]))
        if job.get("status") == "failed" and job.get("attempts", 0) >= 3:
            print("跳過 %s（已失敗 3 次，等待人工處理）" % jid)
            continue
        if not job.get("approved"):
            print("跳過 %s（尚未核准）" % jid)
            continue
        try:
            if not is_due(job, now):
                print("跳過 %s（尚未到 %s）" % (jid, job.get("publish_at")))
                continue
            if is_stale(job, now):
                if job.get("status") != "stale":
                    job["status"] = "stale"
                    job["error"] = ("逾時超過 %d 小時未發布，內容已過期，不自動送出。"
                                    "請人工確認數據是否仍成立，更新 publish_at 後再核准。"
                                    % job.get("max_late_hours", MAX_LATE_HOURS))
                    save(job)
                    log({"id": jid, "at": now.isoformat(timespec="seconds"),
                         "error": job["error"]})
                print("跳過 %s（逾時過久，已標為 stale）" % jid)
                continue
        except PublishError as e:
            print("跳過 %s（%s）" % (jid, e))
            continue

        print("處理 %s" % jid)
        results = {}
        try:
            for platform in job.get("platforms", []):
                gaps = missing_env(platform)
                if gaps:
                    raise PublishError(
                        "%s 尚未設定：缺少 %s。若暫時不發這個平台，"
                        "請把它從 queue 檔的 platforms 移除。"
                        % (platform, "、".join(gaps)))
                if platform == "instagram" and job.get("instagram"):
                    results["instagram"] = publish_instagram(job)
                elif platform == "threads" and job.get("threads"):
                    results["threads"] = publish_threads(job)
            if DRY:
                print("  [DRY] 驗證通過，未發布、未歸檔。")
                continue
            job["status"] = "published"
            job["published_at"] = datetime.now(TPE).isoformat(timespec="seconds")
            job["results"] = results
            archive(job)
            log({"id": jid, "at": job["published_at"], "results": results})
            did += 1
            print("  完成。")
        except PublishError as e:
            failed += 1
            job["status"] = "failed"
            job["attempts"] = job.get("attempts", 0) + 1
            job["error"] = str(e)
            job["failed_at"] = datetime.now(TPE).isoformat(timespec="seconds")
            job["partial_results"] = results
            save(job)
            log({"id": jid, "at": job["failed_at"], "error": str(e),
                 "attempts": job["attempts"]})
            print("  失敗：%s" % e, file=sys.stderr)

    print("完成 %d 則，失敗 %d 則。" % (did, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
