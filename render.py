#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
築日常｜圖卡與 Reels 算圖

在 GitHub Actions 上執行，不在 Claude 的環境。
原因：Claude 讀不進圖片檔（網路被擋，而且一張 6MB 的圖會塞爆工作階段）。
Claude 只寫「版面規格」，實際的裁切、壓字、合成影片都在這裡做。

用法（由 workflow 呼叫）：
    python render.py .pending.json

輸入：publish.py fetch 下載好的原圖，放在 source/<job_id>/
輸出：media/<job_id>/01.jpg … 與 reels.mp4

版面規格寫在 manifest 的 job["render"]，格式見檔尾 SPEC。
"""
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

POST_W, POST_H = 1080, 1350      # 4:5
REEL_W, REEL_H = 1080, 1920      # 9:16
MARGIN = 88

PAL = {
    "ink":      (24, 22, 19),
    "pine":     (31, 41, 36),
    "mist":     (231, 227, 219),
    "gold":     (156, 122, 50),
    "gold_lt":  (201, 161, 74),
    "paper":    (243, 240, 232),
}

SOURCE_DIR = "source"
MEDIA_DIR = "media"

FONT_DIRS = [
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/opentype/noto-cjk",
    "/usr/share/fonts/truetype/dejavu",
]
_FACE_CACHE = {}


# ------------------------------------------------------------------ 字型
def _find(names):
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            for n in names:
                if n.lower() in f.lower():
                    return os.path.join(d, f)
    return None


def _tc_index(path):
    """Noto CJK 是 collection，要挑出繁體那一個 face。"""
    try:
        for i in range(12):
            f = ImageFont.truetype(path, 20, index=i)
            name = (f.getname() or ("", ""))[0] or ""
            if "TC" in name or "Traditional" in name:
                return i
    except Exception:
        pass
    return 0


def font(kind, size):
    key = (kind, size)
    if key in _FACE_CACHE:
        return _FACE_CACHE[key]
    if kind == "serif":
        path = _find(["NotoSerifCJK", "NotoSerifTC", "NotoSerif"])
    elif kind == "sans":
        path = _find(["NotoSansCJK", "NotoSansTC", "NotoSans"])
    else:
        path = _find(["DejaVuSansMono", "DejaVuSans"])
    if not path:
        raise RuntimeError(
            "找不到字型。workflow 要先 apt-get install fonts-noto-cjk fonts-dejavu-core"
        )
    idx = _tc_index(path) if "CJK" in path else 0
    f = ImageFont.truetype(path, size, index=idx)
    _FACE_CACHE[key] = f
    return f


# ------------------------------------------------------------------ 影像
def fit_crop(im, tw, th, focus=None):
    """等比放大後裁成 tw x th。focus 是 [x, y]，0..1，決定保留畫面的哪一部分。"""
    im = im.convert("RGB")
    sw, sh = im.size
    scale = max(tw / sw, th / sh)
    nw, nh = int(round(sw * scale)), int(round(sh * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    fx, fy = (focus or [0.5, 0.5])
    fx = min(max(float(fx), 0.0), 1.0)
    fy = min(max(float(fy), 0.0), 1.0)
    left = int(round((nw - tw) * fx))
    top = int(round((nh - th) * fy))
    return im.crop((left, top, left + tw, top + th))


def scrim(im, strength=0.55, direction="bottom"):
    """壓一層漸層暗幕，讓字站得住。strength 0..1。"""
    w, h = im.size
    grad = Image.new("L", (1, h), 0)
    px = grad.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        if direction == "bottom":
            a = t ** 1.6
        elif direction == "top":
            a = (1 - t) ** 1.6
        else:  # full
            a = 1.0
        px[0, y] = int(255 * a * strength)
    mask = grad.resize((w, h))
    dark = Image.new("RGB", (w, h), PAL["ink"])
    return Image.composite(dark, im, mask)


# ------------------------------------------------------------------ 文字
def wrap_cjk(text, f, maxw):
    """逐字排版並做基本禁則處理。"""
    no_start = "，。、；：！？）」』】’”%》"
    no_end = "（「『【‘“《"
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        trial = cur + ch
        if f.getlength(trial) <= maxw or not cur:
            cur = trial
        else:
            if ch in no_start and cur:
                cur += ch
                lines.append(cur)
                cur = ""
            elif cur and cur[-1] in no_end:
                lines.append(cur[:-1])
                cur = cur[-1] + ch
            else:
                lines.append(cur)
                cur = ch
    if cur:
        lines.append(cur)
    return lines


def draw_lines(d, xy, lines, f, fill, leading):
    x, y = xy
    for ln in lines:
        d.text((x, y), ln, font=f, fill=fill)
        y += leading
    return y


def tracked(d, xy, text, f, fill, tracking):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += f.getlength(ch) + tracking
    return x


def draw_place(im, place, size=20, tracking=2):
    """右下角的地點標。只有查得到地點才傳進來，查不到就不要標。"""
    if not place:
        return im
    w, h = im.size
    d = ImageDraw.Draw(im)
    f = font("sans", size)
    width = sum(f.getlength(c) + tracking for c in place) - tracking
    x = w - MARGIN - width
    y = h - 58 if h == POST_H else h - 76
    # 先描一層暗影，壓在亮處也讀得到
    for dx, dy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        tracked(d, (x + dx, y + dy), place, f, (18, 16, 14), tracking)
    tracked(d, (x, y), place, f, (178, 172, 160), tracking)
    return im


# ------------------------------------------------------------------ 卡片
def card_cover(src, spec, size=(POST_W, POST_H)):
    w, h = size
    im = fit_crop(Image.open(src), w, h, spec.get("focus"))
    im = scrim(im, spec.get("scrim", 0.6), "bottom")
    d = ImageDraw.Draw(im)

    if spec.get("eyebrow"):
        tracked(d, (MARGIN, MARGIN), spec["eyebrow"],
                font("sans", 22), PAL["gold_lt"], 7)

    # 由下往上疊，每一塊都用實際的墨水範圍量高度，避免互相壓到
    y = h - MARGIN

    if spec.get("sub"):
        f = font("sans", 27)
        bb = f.getbbox(spec["sub"])
        y -= (bb[3] - bb[1])
        d.text((MARGIN, y - bb[1]), spec["sub"], font=f, fill=(190, 185, 175))
        y -= 28

    if spec.get("title"):
        f = font("serif", 58)
        lines = wrap_cjk(spec["title"], f, w - MARGIN * 2)
        lead = int(58 * 1.5)
        y -= lead * len(lines)
        draw_lines(d, (MARGIN, y), lines, f, PAL["mist"], lead)
        y -= 34

    if spec.get("big_en"):
        ef = font("sans", 24)
        bb = ef.getbbox(spec["big_en"].upper())
        y -= (bb[3] - bb[1])
        tracked(d, (MARGIN + 4, y - bb[1]), spec["big_en"].upper(),
                ef, PAL["gold_lt"], 6)
        y -= 26

    if spec.get("big"):
        bf = font("serif", spec.get("big_size", 190))
        bb = bf.getbbox(spec["big"])
        y -= (bb[3] - bb[1])
        d.text((MARGIN - bb[0], y - bb[1]), spec["big"], font=bf, fill=PAL["mist"])

    draw_place(im, spec.get("place"))
    return im


def card_photo(src, spec, size=(POST_W, POST_H)):
    w, h = size
    im = fit_crop(Image.open(src), w, h, spec.get("focus"))
    line = spec.get("line")
    if not line:
        draw_place(im, spec.get("place"))
        return im
    im = scrim(im, spec.get("scrim", 0.5), "bottom")
    d = ImageDraw.Draw(im)
    f = font("serif", spec.get("size", 40))
    lines = wrap_cjk(line, f, w - MARGIN * 2)
    lead = int(spec.get("size", 40) * 1.62)
    y = h - MARGIN - lead * len(lines)
    draw_lines(d, (MARGIN, y), lines, f, PAL["mist"], lead)
    draw_place(im, spec.get("place"))
    return im


def card_quote(spec, size=(POST_W, POST_H)):
    w, h = size
    im = Image.new("RGB", (w, h), PAL["pine"] if spec.get("ground") == "pine" else PAL["ink"])
    d = ImageDraw.Draw(im)

    tracked(d, (MARGIN, MARGIN), spec.get("eyebrow", "今 日 一 句"),
            font("sans", 21), PAL["gold_lt"], 9)

    blocks = []
    zf = font("serif", spec.get("size", 54))
    blocks.append(("zh", wrap_cjk(spec["zh"], zf, w - MARGIN * 2),
                   zf, PAL["mist"], int(spec.get("size", 54) * 1.62)))
    if spec.get("en"):
        ef = font("sans", 25)
        blocks.append(("en", wrap_cjk(spec["en"], ef, w - MARGIN * 2),
                       ef, (150, 145, 135), int(25 * 1.7)))
    if spec.get("by"):
        bf = font("sans", 24)
        blocks.append(("by", wrap_cjk(spec["by"], bf, w - MARGIN * 2),
                       bf, (190, 185, 175), int(24 * 1.7)))

    gap = {"zh": 0, "en": 30, "by": 34}
    total = sum(len(b[1]) * b[4] + gap[b[0]] for b in blocks) + 4
    y = h - MARGIN - 70 - total

    for kind, lines, f, col, lead in blocks:
        if kind == "by":
            d.rectangle([MARGIN, y + 6, MARGIN + 96, y + 8], fill=PAL["gold"])
            y += gap[kind]
        else:
            y += gap[kind]
        y = draw_lines(d, (MARGIN, y), lines, f, col, lead)

    if spec.get("note"):
        d.text((MARGIN, h - MARGIN - 26), spec["note"],
               font=font("sans", 21), fill=(120, 116, 108))
    return im


def build_card(job_id, spec, size):
    t = spec.get("type", "photo")
    if t == "quote":
        return card_quote(spec, size)
    src = os.path.join(SOURCE_DIR, job_id, spec["src"])
    if not os.path.exists(src):
        raise FileNotFoundError("找不到原圖：%s" % src)
    if t == "cover":
        return card_cover(src, spec, size)
    return card_photo(src, spec, size)


# ------------------------------------------------------------------ Reels
def build_reels(job_id, frames, out_path, fade=0.6):
    if not frames:
        return None
    tmp = tempfile.mkdtemp(prefix="reels_")
    paths, durs = [], []
    for n, fr in enumerate(frames, 1):
        im = build_card(job_id, fr, (REEL_W, REEL_H))
        p = os.path.join(tmp, "f%02d.png" % n)
        im.save(p)
        paths.append(p)
        durs.append(float(fr.get("sec", 2.5)))

    if len(paths) == 1:
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", str(durs[0]), "-i", paths[0]]
        filt = "[0:v]format=yuv420p[v]"
    else:
        cmd = ["ffmpeg", "-y"]
        for p, dsec in zip(paths, durs):
            cmd += ["-loop", "1", "-t", str(dsec + fade), "-i", p]
        parts, prev, offset = [], "0:v", 0.0
        for i in range(1, len(paths)):
            offset += durs[i - 1] - (fade if i > 1 else 0)
            lab = "x%d" % i
            parts.append("[%s][%d:v]xfade=transition=fade:duration=%s:offset=%s[%s]"
                         % (prev, i, fade, round(offset, 3), lab))
            prev = lab
        parts.append("[%s]format=yuv420p[v]" % prev)
        filt = ";".join(parts)

    cmd += [
        "-f", "lavfi", "-t", str(sum(durs)), "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex", filt,
        "-map", "[v]", "-map", "%d:a" % len(paths),
        "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg 失敗：\n" + res.stderr[-1500:])
    return out_path


# ------------------------------------------------------------------ main
def render_job(job):
    jid = job["id"]
    spec = job.get("render")
    if not spec:
        print("  %s 沒有 render 規格，跳過算圖。" % jid)
        return False

    outdir = os.path.join(MEDIA_DIR, jid)
    os.makedirs(outdir, exist_ok=True)
    made = []

    cards = spec.get("cards", [])
    for n, c in enumerate(cards, 1):
        im = build_card(jid, c, (POST_W, POST_H))
        name = "%02d.jpg" % n
        im.save(os.path.join(outdir, name), "JPEG", quality=92, optimize=True)
        made.append(name)
        print("  算出 %s/%s" % (jid, name))

    if made:
        ig = job.setdefault("instagram", {})
        ig.setdefault("type", "carousel")
        ig["images"] = [{"name": m, "rendered": True} for m in made]

    reels = spec.get("reels")
    if reels and reels.get("frames"):
        out = os.path.join(outdir, "reels.mp4")
        build_reels(jid, reels["frames"], out)
        size_mb = os.path.getsize(out) / 1e6
        print("  算出 %s/reels.mp4（%.1f MB）" % (jid, size_mb))
        job.setdefault("instagram", {})["video"] = {"name": "reels.mp4", "rendered": True}

    return True


def main():
    pending = sys.argv[1] if len(sys.argv) > 1 else ".pending.json"
    if not os.path.exists(pending):
        print("沒有待算的任務。")
        return 0
    with open(pending, encoding="utf-8") as f:
        data = json.load(f)
    jobs = data.get("jobs", [])
    n = 0
    for job in jobs:
        print("算圖 %s" % job.get("id"))
        if render_job(job):
            n += 1
    with open(pending, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("完成 %d 則的算圖。" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())


SPEC = r"""
manifest 裡每個 job 可以加一個 "render"：

"render": {
  "cards": [
    {"type":"cover","src":"yasaka.jpg","focus":[0.5,0.4],"scrim":0.62,
     "eyebrow":"築日常","big":"接","big_en":"joint","big_size":190,
     "title":"不承重的那根柱子","sub":"五重塔的心柱","place":"京都 八坂之塔"},

    {"type":"photo","src":"roofs.jpg","focus":[0.5,0.5],
     "line":"最早，它深深埋進地裡","size":40,"scrim":0.5,"place":"京都"},

    {"type":"photo","src":"eaves.jpg"},

    {"type":"quote","zh":"形永遠追隨機能。","en":"Form ever follows function.",
     "by":"Louis Sullivan，1896","note":"內容僅供資訊與觀察。"}
  ],
  "reels": {
    "frames": [
      {"type":"cover","src":"yasaka.jpg","big":"接","title":"塔的正中間，有一根柱子","sec":2.5},
      {"type":"photo","src":"roofs.jpg","line":"最早　它深深埋進地裡","sec":2.5},
      {"type":"quote","zh":"形永遠追隨機能。","by":"Louis Sullivan，1896","sec":3.5}
    ]
  }
}

- src 指的是 source/<job_id>/ 底下的檔名，由 publish.py fetch 從 Drive 下載。
- focus [x,y] 都是 0..1，決定裁切時保留畫面的哪個部分。0.5,0.4 = 稍微偏上。
- place 是右下角的地點標。**只有查得到地點才寫**，查不到就不要放這個欄位——寧可不標，也不要標錯。
- 算完之後 job["instagram"]["images"] 會被改寫成算出來的檔名，發布時用這些。
"""
