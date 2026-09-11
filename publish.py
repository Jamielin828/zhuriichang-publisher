#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
築日常｜自動發布器 v2

和 v1 的差別：發文任務不再放在 repo 裡，改成放在 Google Drive 的一份
公開 manifest.json。Claude 每週把成品寫進 Drive、更新 manifest，
這支程式再由 GitHub Actions 定時去抓。

原因：Claude 的執行環境不能寫入 GitHub，但可以寫入 Drive。
GitHub Actions 對自己的 repo 有寫入權，所以由它把圖片 commit 進 repo，
產生 raw.githubusercontent 的公開網址給 Meta 抓。

用法（由 workflow 呼叫，不要手動跑）：
    python publish.py fetch    # 從 Drive 抓到期任務的媒體，寫出 .pending.json
    python publish.py post     # 用 repo 裡的圖發文，寫出 published/log.jsonl

環境變數：
    MANIFEST_FILE_ID   Drive 上 manifest.json 的檔案 ID（公開可讀）
    MEDIA_BASE_URL     https://raw.githubusercontent.com/<user>/<repo>/main/
    IG_USER_ID         Instagram 專業帳號 user id
    IG_TOKEN           Instagram 長效 access token
    TH_USER_ID         Threads user id（選填，沒填就用 token 自動問出來）
    TH_TOKEN           Threads 長效 access token（選填）
    DRY_RUN=1          只驗證不發文
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))

IG_API = "https://graph.instagram.com/v23.0"
TH_API = "https://graph.threads.net/v1.0"

IG_CAPTION_MAX = 2200
TH_TEXT_MAX = 500
IG_CAROUSEL_MIN = 2
IG_CAROUSEL_MAX = 10
MAX_LATE_HOURS = 6
MAX_ATTEMPTS = 3

PENDING = ".pending.json"
LOG = "published/log.jsonl"
MEDIA_DIR = "media"
SOURCE_DIR = "source"

DRY = os.environ.get("DRY_RUN") == "1"


class PublishError(Exception):
    pass


# ------------------------------------------------------------------ 共用
def http(url, data=None, method=None, timeout=90, raw=False):
    if data is not None and not isinstance(data, bytes):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "zhuriichang-publisher/2")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise PublishError("HTTP %s %s — %s" % (e.code, url.split("?")[0], detail))
    except Exception as e:
        raise PublishError("network error %s — %s" % (url.split("?")[0], e))
    if raw:
        return body
    return json.loads(body.decode("utf-8"))


def need(name):
    v = os.environ.get(name)
    if not v:
        raise PublishError("缺少環境變數 %s" % name)
    return v


def now_tpe():
    return datetime.now(TPE)


def when_of(job):
    s = job.get("publish_at", "")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise PublishError("publish_at 格式不對：%r" % s)
    return d if d.tzinfo else d.replace(tzinfo=TPE)


def is_due(job, now):
    return now >= when_of(job)


def is_stale(job, now):
    late = job.get("max_late_hours", MAX_LATE_HOURS)
    return now > when_of(job) + timedelta(hours=late)


def _log_records():
    if not os.path.exists(LOG):
        return []
    out = []
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def done_ids():
    """整則已經結案的 id：全部平台發完、或標為 stale／放棄。"""
    return {r.get("id") for r in _log_records()
            if r.get("status") in ("published", "stale", "abandoned")}


def done_platforms(jid):
    """這一則已經成功發出去的平台。

    重要：IG 發成功但 Threads 失敗時，整則會重試。沒有這層紀錄的話，
    重試會把 IG 再發一次，變成重複貼文。
    """
    done = set()
    for r in _log_records():
        if r.get("id") != jid:
            continue
        if r.get("status") == "platform_published" and r.get("platform"):
            done.add(r["platform"])
        elif r.get("status") == "published":
            done.update(r.get("platforms") or [])
    return done


def write_log(rec):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rec.setdefault("at", now_tpe().isoformat())
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ Drive
def drive_get(file_id, timeout=120):
    """抓一個公開的 Drive 檔案。小檔案不會有病毒掃描確認頁。"""
    url = "https://drive.google.com/uc?export=download&id=" + urllib.parse.quote(file_id)
    body = http(url, timeout=timeout, raw=True)
    head = body[:200].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise PublishError(
            "Drive 檔案 %s 回傳的是網頁，不是檔案。"
            "多半是沒有設成「知道連結的任何人都可以檢視」。" % file_id
        )
    return body


def load_manifest():
    fid = need("MANIFEST_FILE_ID")
    body = drive_get(fid, timeout=60)
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise PublishError("manifest.json 不是合法的 JSON：%s" % e)


# ------------------------------------------------------------------ fetch
def rel_media_path(job_id, name):
    return "%s/%s/%s" % (MEDIA_DIR, job_id, name)


def cmd_fetch():
    now = now_tpe()
    print("現在時間（台北）：%s" % now.isoformat(timespec="seconds"))

    manifest = load_manifest()
    jobs = manifest.get("jobs", [])
    print("manifest 共 %d 則" % len(jobs))

    already = done_ids()
    picked = []

    # 兩段式：還沒算圖的先算（不管核不核准，讓人看得到成品再決定）；
    # 已核准且到期的才會真的送出。
    for job in jobs:
        jid = job.get("id") or "(無 id)"
        if jid in already:
            continue

        rendered = os.path.isdir(os.path.join(MEDIA_DIR, jid)) and \
            os.listdir(os.path.join(MEDIA_DIR, jid))
        needs_render = bool(job.get("render")) and not rendered

        can_post = True
        if not job.get("approved"):
            can_post = False
            if not needs_render:
                print("跳過 %s（尚未核准，成品已算好等你看）" % jid)
                continue
        elif not is_due(job, now):
            can_post = False
            if not needs_render:
                print("跳過 %s（尚未到 %s）" % (jid, job.get("publish_at")))
                continue
        elif is_stale(job, now):
            print("跳過 %s（逾時超過 %d 小時，標為 stale）"
                  % (jid, job.get("max_late_hours", MAX_LATE_HOURS)))
            write_log({"id": jid, "status": "stale",
                       "publish_at": job.get("publish_at")})
            continue

        job["_post"] = can_post
        print("處理 %s（%s）" % (jid, "算圖並發布" if can_post else "只算圖，不發布"))
        picked.append(job)

    if not picked:
        print("沒有需要算圖或發布的任務。")
        if os.path.exists(PENDING):
            os.remove(PENDING)
        return

    for job in picked:
        jid = job["id"]
        render = job.get("render")
        # 有 render 規格的，下載的是「原圖」放進 source/；成品由 render.py 算出來放進 media/
        base = os.path.join(SOURCE_DIR if render else MEDIA_DIR, jid)
        os.makedirs(base, exist_ok=True)
        for item in media_items(job):
            path = os.path.join(base, item["name"])
            if os.path.exists(path) and os.path.getsize(path) > 0:
                print("  已有 %s" % path)
                continue
            print("  下載 %s ← Drive %s" % (path, item["drive_id"]))
            data = drive_get(item["drive_id"])
            with open(path, "wb") as f:
                f.write(data)
            print("       %d bytes" % len(data))

    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump({"jobs": picked}, f, ensure_ascii=False, indent=2)
    print("待發 %d 則，已寫入 %s" % (len(picked), PENDING))


def media_items(job):
    """把一個任務裡所有需要從 Drive 下載的檔案列出來。

    有 render 規格時，要下載的是 render.sources 裡的原圖；
    沒有的話，就是 instagram / threads 直接指定的成品。
    只回傳帶 drive_id 的項目——rendered 的檔案是算出來的，不用下載。
    """
    render = job.get("render")
    if render:
        return [s for s in render.get("sources", []) if s.get("drive_id")]
    out = []
    ig = job.get("instagram") or {}
    out.extend(ig.get("images", []))
    if ig.get("video"):
        out.append(ig["video"])
    th = job.get("threads") or {}
    out.extend(th.get("images", []))
    return [i for i in out if i.get("drive_id")]


# ------------------------------------------------------------------ 網址
def media_url(job_id, name):
    base = need("MEDIA_BASE_URL")
    if not base.endswith("/"):
        base += "/"
    return base + urllib.parse.quote(rel_media_path(job_id, name))


def wait_public(url, tries=20, delay=15):
    """raw.githubusercontent 有 CDN 延遲，發文前先確認圖抓得到。"""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("User-Agent", "zhuriichang-publisher/2")
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        print("    等待 %s 上線（%d/%d）" % (url.rsplit("/", 1)[-1], i + 1, tries))
        time.sleep(delay)
    raise PublishError("圖片網址遲遲抓不到：%s" % url)


# ------------------------------------------------------------------ IG
def ig(path, params, method="POST"):
    params = dict(params)
    params["access_token"] = need("IG_TOKEN")
    url = "%s/%s" % (IG_API, path)
    if method == "GET":
        return http(url + "?" + urllib.parse.urlencode(params), method="GET")
    return http(url, data=params)


def ig_wait(container_id, label, tries=30, delay=6):
    for _ in range(tries):
        r = ig(container_id, {"fields": "status_code,status"}, method="GET")
        code = r.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise PublishError("IG 容器 %s (%s) 失敗：%s"
                               % (container_id, label, r.get("status")))
        time.sleep(delay)
    raise PublishError("IG 容器 %s (%s) 逾時未就緒" % (container_id, label))


def publish_instagram(job):
    spec = job["instagram"]
    uid = need("IG_USER_ID")
    jid = job["id"]
    caption = spec.get("caption", "")
    if len(caption) > IG_CAPTION_MAX:
        raise PublishError("IG caption 超過 %d 字元（目前 %d）"
                           % (IG_CAPTION_MAX, len(caption)))

    kind = spec.get("type", "carousel")

    if kind == "reels":
        video = spec.get("video")
        if not video:
            raise PublishError("Reels 任務沒有影片")
        url = media_url(jid, video["name"])
        if DRY:
            print("  [DRY] IG Reels，caption %d 字元" % len(caption))
            print("        " + url)
            return {"dry_run": True}
        wait_public(url)
        cid = ig("%s/media" % uid,
                 {"media_type": "REELS", "video_url": url, "caption": caption})["id"]
        ig_wait(cid, "reels", tries=60, delay=10)
        res = ig("%s/media_publish" % uid, {"creation_id": cid})
        print("  IG Reels 已發布：%s" % res.get("id"))
        return res

    images = spec.get("images", [])
    if not images:
        raise PublishError("IG 任務沒有任何圖片")
    if len(images) > IG_CAROUSEL_MAX:
        raise PublishError("IG 輪播最多 %d 張（目前 %d）"
                           % (IG_CAROUSEL_MAX, len(images)))
    urls = [media_url(jid, i["name"]) for i in images]

    if DRY:
        print("  [DRY] IG 輪播 %d 張，caption %d 字元" % (len(urls), len(caption)))
        for u in urls:
            print("        " + u)
        return {"dry_run": True}

    for u in urls:
        wait_public(u)

    if len(urls) == 1:
        cid = ig("%s/media" % uid, {"image_url": urls[0], "caption": caption})["id"]
        ig_wait(cid, "single")
        res = ig("%s/media_publish" % uid, {"creation_id": cid})
    else:
        if len(urls) < IG_CAROUSEL_MIN:
            raise PublishError("IG 輪播至少 %d 張" % IG_CAROUSEL_MIN)
        children = []
        for n, u in enumerate(urls, 1):
            c = ig("%s/media" % uid,
                   {"image_url": u, "is_carousel_item": "true"})["id"]
            print("  IG 子容器 %d/%d → %s" % (n, len(urls), c))
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


# ------------------------------------------------------------------ Threads
def th(path, params, method="POST"):
    params = dict(params)
    params["access_token"] = need("TH_TOKEN")
    url = "%s/%s" % (TH_API, path)
    if method == "GET":
        return http(url + "?" + urllib.parse.urlencode(params), method="GET")
    return http(url, data=params)


def th_me_id():
    """沒有 TH_USER_ID 時，直接用 token 問出帳號編號。

    token 本身就代表那個帳號，所以這個值是可以推導的，不必人工填。
    """
    if not hasattr(th_me_id, "_cached"):
        me = th("me", {"fields": "id,username"}, method="GET")
        th_me_id._cached = me.get("id")
        print("  Threads 帳號：%s（%s）" % (me.get("username"), me.get("id")))
    if not th_me_id._cached:
        raise PublishError("問不到 Threads 帳號編號，token 可能無效。")
    return th_me_id._cached


def th_wait(container_id, tries=20, delay=5):
    for _ in range(tries):
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
    uid = os.environ.get("TH_USER_ID") or th_me_id()
    jid = job["id"]
    text = spec.get("text", "")
    if len(text) > TH_TEXT_MAX:
        raise PublishError("Threads 內文超過 %d 字元（目前 %d）"
                           % (TH_TEXT_MAX, len(text)))
    images = spec.get("images", [])
    urls = [media_url(jid, i["name"]) for i in images]

    if DRY:
        print("  [DRY] Threads %d 字元，%d 張圖" % (len(text), len(urls)))
        return {"dry_run": True}

    for u in urls:
        wait_public(u)

    if not urls:
        cid = th("%s/threads" % uid, {"media_type": "TEXT", "text": text})["id"]
    elif len(urls) == 1:
        cid = th("%s/threads" % uid,
                 {"media_type": "IMAGE", "image_url": urls[0], "text": text})["id"]
    else:
        kids = []
        for u in urls[:20]:
            k = th("%s/threads" % uid,
                   {"media_type": "IMAGE", "image_url": u,
                    "is_carousel_item": "true"})["id"]
            kids.append(k)
        cid = th("%s/threads" % uid,
                 {"media_type": "CAROUSEL", "children": ",".join(kids),
                  "text": text})["id"]
    th_wait(cid)
    time.sleep(30)
    res = th("%s/threads_publish" % uid, {"creation_id": cid})
    print("  Threads 已發布：%s" % res.get("id"))
    return res


# TH_USER_ID 是選填的：沒填就用 token 問出來。
NEEDED_ENV = {
    "instagram": ("IG_USER_ID", "IG_TOKEN"),
    "threads": ("TH_TOKEN",),
}


def missing_env(platform):
    return [k for k in NEEDED_ENV.get(platform, ()) if not os.environ.get(k)]


# ------------------------------------------------------------------ post
def cmd_post():
    if not os.path.exists(PENDING):
        print("沒有待發任務。")
        return 0
    with open(PENDING, encoding="utf-8") as f:
        jobs = json.load(f).get("jobs", [])

    ok = fail = skipped = 0
    for job in jobs:
        jid = job["id"]
        if not job.get("_post"):
            print("%s：只算圖，不發布（成品已 commit，等你核准）" % jid)
            skipped += 1
            continue
        print("處理 %s" % jid)
        results = {}
        already_sent = done_platforms(jid)
        if already_sent:
            print("  已發過：%s（不再重發）" % "、".join(sorted(already_sent)))
        try:
            for platform in job.get("platforms", []):
                if platform in already_sent:
                    continue
                gap = missing_env(platform)
                if gap:
                    raise PublishError("%s 缺少 %s" % (platform, "、".join(gap)))
                if platform == "instagram":
                    res = publish_instagram(job)
                elif platform == "threads":
                    res = publish_threads(job)
                else:
                    raise PublishError("不認識的平台：%s" % platform)
                results[platform] = res
                if not DRY:
                    # 每發成功一個平台就立刻記一筆，後面失敗也不會重發這個
                    write_log({"id": jid, "status": "platform_published",
                               "platform": platform,
                               "post_id": (res or {}).get("id")})
        except PublishError as e:
            fail += 1
            print("  失敗：%s" % e)
            if not DRY:
                write_log({"id": jid, "status": "failed", "error": str(e),
                           "publish_at": job.get("publish_at")})
            continue
        ok += 1
        if DRY:
            print("  [DRY] 驗證通過，未發布。")
        else:
            write_log({"id": jid, "status": "published",
                       "publish_at": job.get("publish_at"),
                       "platforms": job.get("platforms"),
                       "results": {k: (v or {}).get("id") for k, v in results.items()}})

    print("發布 %d 則，失敗 %d 則，待核准 %d 則。" % (ok, fail, skipped))
    return 1 if fail and not ok else 0


# ------------------------------------------------------------------ main
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    try:
        if cmd == "fetch":
            cmd_fetch()
            return 0
        if cmd == "post":
            return cmd_post()
        print("用法：publish.py [fetch|post]")
        return 2
    except PublishError as e:
        print("錯誤：%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
