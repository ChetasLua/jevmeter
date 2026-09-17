"""Frame renderer. Reads <work>/timeline.json, <work>/edl.json, <work>/render_config.json.

Run directly for one slice of the EDL (the CLI starts several of these in parallel):
    python -m jevmeter.render <work> <out.mp4> <seg_lo> <seg_hi>
"""
import bisect
import json
import math
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from .util import clamp, ease, ffmpeg, font, hex_rgb, mix

W, H, FPS = 1920, 1080, 30
FX, FY, FW, FH = 330, 64, 1260, 709
LP = (24, 64, 306, 773)
RP = (1614, 64, 1896, 773)
BOT = (24, 792, 1896, 1056)
DW, DH = 1600, 900
BG_C, PANEL, LINE, DIM, TEXT, JEV = (6, 8, 13), (13, 16, 24), (38, 44, 58), (120, 128, 145), (236, 239, 245), (190, 255, 120)
NEUTRAL = (225, 228, 235)


def text_w(s, f):
    return f.getbbox(s)[2] if s else 0


def rrect(d, box, r, **kw):
    x0, y0, x1, y1 = box
    if x1 < x0 or y1 < y0:
        return
    d.rounded_rectangle(box, r, **kw)


def layer():
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def comp(base, lay, alpha=1.0):
    if alpha <= 0:
        return
    if alpha < 1:
        a = np.array(lay)
        a[..., 3] = (a[..., 3] * alpha).astype(np.uint8)
        lay = Image.fromarray(a)
    base.alpha_composite(lay)


def wrap_words(words, f, maxw):
    lines, cur = [], []
    for i, w in enumerate(words):
        if cur and text_w(" ".join([words[j] for j in cur] + [w]), f) > maxw:
            lines.append(cur)
            cur = [i]
        else:
            cur.append(i)
    if cur:
        lines.append(cur)
    return lines


class Ctx:
    """Everything a frame needs: data, colours, sprites, cumulative stats."""

    def __init__(self, work):
        self.work = work
        self.cfg = json.load(open(os.path.join(work, "render_config.json")))
        tl = json.load(open(os.path.join(work, "timeline.json")))
        self.video = self.cfg["video"]
        self.preset = self.cfg["preset"]
        self.qs = [(q["key"], q["label"]) for q in self.preset["questions"]]
        self.qneutral = {q["key"] for q in self.preset["questions"] if not q.get("index")}
        self.speakers = tl["speakers"]
        self.single = len(self.speakers) == 1
        self.labels = {sp: self.cfg.get("labels", {}).get(sp, sp) for sp in self.speakers}
        cols = self.cfg.get("colors") or ["#ff4848", "#488cff"]
        self.col = {sp: hex_rgb(cols[i % len(cols)]) for i, sp in enumerate(self.speakers)}
        self.side = {sp: ("L" if i == 0 else "R") for i, sp in enumerate(self.speakers)}
        self.summary = tl["summary"]
        self.thr = self.summary["thresholds"]
        self.idx = self.summary["index_keys"]
        self.price = self.cfg.get("price_per_million_input", 0.042)
        self.index_label = self.preset.get("index_label", "BS INDEX")
        self.S = tl["sentences"]  # sorted by end
        self.ends = [s["end"] for s in self.S]
        by_start = sorted(range(len(self.S)), key=lambda i: self.S[i]["start"])
        self.by_start = by_start
        self.starts = [self.S[i]["start"] for i in by_start]
        self.cum = {}
        for sp in self.speakers:
            acc = {k: 0.0 for k, _ in self.qs}
            acc["idx"] = 0.0
            n, rows = 0, []
            for s in self.S:
                if s["speaker"] == sp:
                    n += 1
                    for k, _ in self.qs:
                        acc[k] += s["scores"][k]
                    acc["idx"] += self.ival(s)
                rows.append((n, dict(acc)))
            self.cum[sp] = rows
        self.tok = np.cumsum([s["tokens"] for s in self.S]).tolist()
        self._sprites()

    def ival(self, s):
        return sum(s["scores"][k] for k in self.idx) / max(1, len(self.idx))

    # ------------------------------------------------------------ data queries
    def stats(self, T):
        i = bisect.bisect_right(self.ends, T)
        out = {}
        for sp in self.speakers:
            if i == 0:
                out[sp] = (0, {**{k: 0 for k, _ in self.qs}, "idx": 0})
                continue
            n, acc = self.cum[sp][i - 1]
            out[sp] = (n, {k: (v / n if n else 0) for k, v in acc.items()})
        return i, out, (self.tok[i - 1] if i else 0)

    def last_scored(self, T, sp=None):
        i = bisect.bisect_right(self.ends, T) - 1
        while i >= 0 and sp and self.S[i]["speaker"] != sp:
            i -= 1
        return self.S[i] if i >= 0 else None

    def speaking(self, T):
        i = bisect.bisect_right(self.starts, T + 0.05) - 1
        for j in range(i, max(-1, i - 6), -1):
            s = self.S[self.by_start[j]]
            if s["start"] - 0.05 <= T <= s["end"] + 0.6:
                return s
        return None

    def flag_of(self, s):
        hits = [(k, s["scores"][k]) for k, _ in self.qs if s["scores"][k] >= self.thr[k]]
        return max(hits, key=lambda kv: kv[1]) if hits else None

    def flags(self, T, sp, life=2.6):
        out, i = [], bisect.bisect_right(self.ends, T) - 1
        while i >= 0 and T - self.ends[i] < life and len(out) < 3:
            s = self.S[i]
            if s["speaker"] == sp:
                f = self.flag_of(s)
                if f:
                    out.append((dict(self.qs)[f[0]].upper(), f[1], T - self.ends[i], f[0] in self.qneutral))
            i -= 1
        return out

    def recent_flag_age(self, T, window=0.6):
        i = bisect.bisect_right(self.ends, T) - 1
        while i >= 0 and T - self.ends[i] < window:
            if self.flag_of(self.S[i]):
                return T - self.ends[i]
            i -= 1
        return None

    # ------------------------------------------------------------ sprites
    def _sprites(self):
        bg = Image.new("RGBA", (W, H), BG_C + (255,))
        a = np.array(bg).astype(np.float32)
        yy, xx = np.mgrid[0:H, 0:W]
        a[..., :3] += np.exp(-(((xx - W / 2) / 1100) ** 2 + ((yy - 420) / 650) ** 2))[..., None] * np.array([10, 12, 20])
        bg = Image.fromarray(a.clip(0, 255).astype(np.uint8))
        grid = layer()
        gd = ImageDraw.Draw(grid)
        for x in range(0, W, 48):
            gd.line((x, 0, x, H), fill=(255, 255, 255, 7))
        for y in range(0, H, 48):
            gd.line((0, y, W, y), fill=(255, 255, 255, 7))
        bg.alpha_composite(grid)
        pan = layer()
        d = ImageDraw.Draw(pan)
        for box in (LP, RP, BOT):
            rrect(d, box, 18, fill=PANEL + (235,), outline=LINE + (255,))
        bg.alpha_composite(pan)
        self.bg = bg

        def glow(box, col, width=6, blur=16, pad=40):
            x0, y0, x1, y1 = box
            im = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), (0, 0, 0, 0))
            ImageDraw.Draw(im).rounded_rectangle((pad, pad, pad + x1 - x0, pad + y1 - y0), 18, outline=col + (255,), width=width)
            g = im.filter(ImageFilter.GaussianBlur(blur))
            ImageDraw.Draw(g).rounded_rectangle((pad, pad, pad + x1 - x0, pad + y1 - y0), 18, outline=col + (230,), width=2)
            full = layer()
            full.alpha_composite(g, (x0 - pad, y0 - pad))
            return full

        def blob(cx, cy, rad, col, alpha=70):
            m = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * rad ** 2)))
            arr = np.zeros((H, W, 4), np.uint8)
            arr[..., :3] = col
            arr[..., 3] = (m * alpha).astype(np.uint8)
            return Image.fromarray(arr)

        self.panel_glow = {sp: glow(LP if self.side[sp] == "L" else RP, self.col[sp]) for sp in self.speakers}
        self.foot_glow = {sp: glow((FX, FY, FX + FW, FY + FH), self.col[sp], 4, 22) for sp in self.speakers}
        self.amb = {sp: blob(260 if self.side[sp] == "L" else W - 260, 420, 520, self.col[sp]) for sp in self.speakers}
        beam = np.zeros((140, FW, 4), np.uint8)
        beam[..., :3] = JEV
        beam[..., 3] = np.concatenate([np.linspace(0, 1, 134) ** 3 * 90, np.full(6, 255)])[:, None].astype(np.uint8)
        self.beam = Image.fromarray(beam)
        fy, fx = np.mgrid[0:FH, 0:FW]
        v = ((fx - FW / 2) / (FW / 2)) ** 2 + ((fy - FH / 2) / (FH / 2)) ** 2
        vig = np.zeros((FH, FW, 4), np.uint8)
        vig[..., 3] = (np.clip(v - 0.55, 0, 1) ** 1.5 * 150).astype(np.uint8)
        self.vig = Image.fromarray(vig)
        self.mask = Image.new("L", (FW, FH), 0)
        ImageDraw.Draw(self.mask).rounded_rectangle((0, 0, FW - 1, FH - 1), 18, fill=255)


# ---------------------------------------------------------------- footage
class Clip:
    def __init__(self, video, start, dur):
        self.p = subprocess.Popen([ffmpeg(), "-loglevel", "error", "-ss", f"{start:.3f}", "-i", video, "-t", f"{dur + 0.5:.3f}",
                                   "-vf", f"fps={FPS},scale={DW}:{DH}:force_original_aspect_ratio=decrease,pad={DW}:{DH}:(ow-iw)/2:(oh-ih)/2",
                                   "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
        self.last = Image.new("RGB", (DW, DH))

    def next(self):
        b = self.p.stdout.read(DW * DH * 3)
        if len(b) == DW * DH * 3:
            self.last = Image.frombuffer("RGB", (DW, DH), b)
        return self.last

    def close(self):
        self.p.kill()


def still(video, t, cache_dir):
    path = os.path.join(cache_dir, f"still_{int(t * 10)}.png")
    if not os.path.exists(path):
        os.makedirs(cache_dir, exist_ok=True)
        subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-ss", f"{max(0, t):.2f}", "-i", video, "-frames:v", "1",
                        "-vf", f"scale={DW}:{DH}:force_original_aspect_ratio=decrease,pad={DW}:{DH}:(ow-iw)/2:(oh-ih)/2", path])
    return Image.open(path).convert("RGB") if os.path.exists(path) else Image.new("RGB", (DW, DH))


def is_split(fr):
    """Broadcast debate split screens have a bright vertical divider in the middle."""
    a = np.asarray(fr.resize((320, 180)), dtype=np.float32).mean(axis=2)
    band = a[20:150, 158:162].mean()
    side = np.r_[a[20:150, 148:152].ravel(), a[20:150, 168:172].ravel()].mean()
    return band > 150 and band - side > 60


def camera(fr, z, cx, cy=0.5, blur=0.0):
    w, h = DW / z, DH / z
    x0 = clamp(cx * DW - w / 2, 0, DW - w)
    y0 = clamp(cy * DH - h / 2, 0, DH - h)
    out = fr.resize((FW, FH), Image.BICUBIC, box=(x0, y0, x0 + w, y0 + h))
    if blur > 0.5:
        a = np.asarray(out, dtype=np.float32)
        r = int(blur)
        cs = np.cumsum(np.pad(a, ((0, 0), (r + 1, r), (0, 0)), mode="edge"), axis=1)
        out = Image.fromarray(((cs[:, 2 * r + 1:] - cs[:, :-2 * r - 1]) / (2 * r + 1))[:, :FW].clip(0, 255).astype(np.uint8))
    return out


def grade(fr):
    return ImageEnhance.Color(ImageEnhance.Contrast(fr).enhance(1.07)).enhance(1.08)


class Smooth:
    def __init__(self):
        self.v = {}

    def go(self, k, target, rate=0.18):
        cur = self.v.get(k, target)
        cur += (target - cur) * rate
        self.v[k] = cur
        return cur


# ---------------------------------------------------------------- UI pieces
def draw_panel(c, img, sp, st, act, flash, flags, disp):
    x0, y0, x1, y1 = LP if c.side[sp] == "L" else RP
    col = c.col[sp]
    if act > 0.01:
        comp(img, c.panel_glow[sp], act)
    lay = layer()
    d = ImageDraw.Draw(lay)
    n, _ = st
    cx = (x0 + x1) // 2
    rrect(d, (x0 + 18, y0 + 18, x0 + 60 + 60 * act, y0 + 24), 3, fill=col)
    name = c.labels[sp]
    nf = font(42 if text_w(name, font(42, "Heavy")) < 250 else 30, "Heavy")
    d.text((x0 + 18, y0 + 34), name, font=nf, fill=TEXT)
    d.text((x0 + 18, y0 + 84), "● SPEAKING" if act > 0.5 else "● live", font=font(18, "Bold" if act > 0.5 else "Medium"), fill=col if act > 0.5 else DIM)
    gy = y0 + 225
    iv = disp[f"{sp}:idx"]
    hs = 300
    halo = Image.new("RGBA", (hs, hs), (0, 0, 0, 0))
    ImageDraw.Draw(halo).arc((hs / 2 - 92, hs / 2 - 92, hs / 2 + 92, hs / 2 + 92), 135, 135 + 270 * clamp(iv), fill=col + (200,), width=26)
    hl = layer()
    hl.alpha_composite(halo.filter(ImageFilter.GaussianBlur(14)), (int(cx - hs / 2), int(gy - hs / 2)))
    comp(img, hl, 0.3 + 0.55 * max(act, flash))
    d.arc((cx - 92, gy - 92, cx + 92, gy + 92), 135, 405, fill=(40, 46, 60), width=16)
    if iv > 0.001:
        d.arc((cx - 92, gy - 92, cx + 92, gy + 92), 135, 135 + 270 * clamp(iv), fill=col, width=16)
    for t in range(11):
        ang = math.radians(135 + 27 * t)
        r2 = 114 if t % 5 else 120
        d.line((cx + math.cos(ang) * 108, gy + math.sin(ang) * 108, cx + math.cos(ang) * r2, gy + math.sin(ang) * r2), fill=(90, 98, 115), width=2)
    val = f"{iv * 100:.1f}"
    f = font(64, "Bold", mono=True)
    d.text((cx - text_w(val, f) / 2, gy - 48), val, font=f, fill=mix(TEXT, col, flash * 0.9))
    lab = c.index_label
    d.text((cx - text_w(lab, font(17, "Bold")) / 2, gy + 34), lab, font=font(17, "Bold"), fill=DIM)
    rows = len(c.qs)
    step = min(52, int(300 / max(1, rows)))
    y = y0 + 345
    for k, label in c.qs:
        v = disp[f"{sp}:{k}"]
        d.text((x0 + 18, y), label, font=font(19, "Medium"), fill=(200, 205, 215))
        pv = f"{v * 100:.0f}%"
        d.text((x1 - 18 - text_w(pv, font(19, "Semibold", True)), y), pv, font=font(19, "Semibold", True), fill=TEXT)
        rrect(d, (x0 + 18, y + 28, x1 - 18, y + 36), 4, fill=(32, 37, 50))
        bc = NEUTRAL if k in c.qneutral else col
        L = max(8, (x1 - x0 - 36) * v)
        if v > 0:
            rrect(d, (x0 + 18, y + 28, x0 + 18 + L, y + 36), 4, fill=mix(bc, (20, 22, 30), 0.35))
            rrect(d, (x0 + 18 + L - 26, y + 28, x0 + 18 + L, y + 36), 4, fill=bc)
        y += step
    d.text((x0 + 18, y + 2), "sentences scored", font=font(17, "Medium"), fill=DIM)
    ns = f"{n:,}"
    d.text((x1 - 18 - text_w(ns, font(24, "Bold", True)), y - 2), ns, font=font(24, "Bold", True), fill=TEXT)
    comp(img, lay)
    fy = y1 - 18
    for label, pv, age, neutral in flags:
        a = clamp(age / 0.12) * clamp((2.6 - age) / 0.35)
        if a <= 0:
            continue
        pop = 1 + 0.12 * math.exp(-age * 9) * math.sin(age * 30)
        h = 58
        mid = (x0 + x1) / 2
        half = (x1 - x0 - 28) / 2 * pop
        fill = NEUTRAL if neutral else col
        gs = Image.new("RGBA", (int(2 * half) + 60, h + 60), (0, 0, 0, 0))
        rrect(ImageDraw.Draw(gs), (30, 30, 30 + 2 * half, 30 + h), 12, fill=fill + (160,))
        g = layer()
        g.alpha_composite(gs.filter(ImageFilter.GaussianBlur(12)), (int(mid - half - 30), int(fy - h - 30)))
        comp(img, g, a * 0.9)
        ch = layer()
        cd = ImageDraw.Draw(ch)
        rrect(cd, (mid - half, fy - h, mid + half, fy), 12, fill=fill + (255,))
        tc = (15, 18, 26) if neutral else (255, 255, 255)
        lf = font(17 if text_w(label, font(17, "Heavy")) < 240 else 14, "Heavy")
        cd.text((x0 + 28, fy - h + 9), label, font=lf, fill=tc)
        cd.text((x0 + 28, fy - h + 29), f"Jev p = {pv:.2f}", font=font(18, "Semibold", True), fill=tc)
        comp(img, ch, a)
        fy -= h + 10


def draw_feed(c, img, T):
    """Single-speaker mode: the right panel lists the latest flagged sentences."""
    x0, y0, x1, y1 = RP
    lay = layer()
    d = ImageDraw.Draw(lay)
    d.text((x0 + 18, y0 + 34), "JEV FEED", font=font(34, "Heavy"), fill=TEXT)
    d.text((x0 + 18, y0 + 78), "latest flagged sentences", font=font(17, "Medium"), fill=DIM)
    i = bisect.bisect_right(c.ends, T) - 1
    y = y0 + 120
    while i >= 0 and y < y1 - 90:
        s = c.S[i]
        f = c.flag_of(s)
        if f:
            age = T - s["end"]
            a = clamp(age / 0.25)
            col = NEUTRAL if f[0] in c.qneutral else c.col[s["speaker"]]
            rrect(d, (x0 + 14, y, x1 - 14, y + 100), 10, fill=(24, 28, 38, int(255 * a)))
            rrect(d, (x0 + 14, y, x0 + 20, y + 100), 3, fill=col + (int(255 * a),))
            d.text((x0 + 30, y + 8), f"{dict(c.qs)[f[0]].upper()}  {f[1]:.2f}", font=font(15, "Heavy"), fill=col + (int(255 * a),))
            words = s["text"].split()
            lines = wrap_words(words, font(16, "Medium"), x1 - x0 - 60)[:3]
            for n, ln in enumerate(lines):
                t = " ".join(words[j] for j in ln) + ("…" if n == 2 and len(wrap_words(words, font(16, "Medium"), x1 - x0 - 60)) > 3 else "")
                d.text((x0 + 30, y + 30 + n * 21), t, font=font(16, "Medium"), fill=(210, 214, 222, int(255 * a)))
            y += 112
        i -= 1
    comp(img, lay)


def draw_header(c, img, T, disp):
    lay = layer()
    d = ImageDraw.Draw(lay)
    pulse = 0.5 + 0.5 * math.sin(T * 6)
    d.ellipse((28, 24, 46, 42), fill=mix(JEV, (40, 80, 30), pulse * 0.5))
    d.text((56, 19), "JEV", font=font(24, "Heavy"), fill=TEXT)
    sub = c.preset.get("header", f"by TypeSafe  ·  {len(c.qs)} yes/no questions on every sentence")
    d.text((106, 22), sub, font=font(20, "Medium"), fill=DIM)
    toks = int(disp["tok"])
    parts = [(f"{int(disp['calls']):,}", " calls"), (f"{toks:,}", " tokens"), (f"${toks / 1e6 * c.price:.4f}", "")]
    f1, f2 = font(22, "Bold", True), font(19, "Medium")
    x = W - 30
    for v, l in reversed(parts):
        x -= text_w(l, f2)
        d.text((x, 23), l, font=f2, fill=DIM)
        x -= text_w(v, f1)
        d.text((x, 20), v, font=f1, fill=JEV if v.startswith("$") else TEXT)
        x -= 28
    comp(img, lay)


def draw_bottom(c, img, T):
    lay = layer()
    d = ImageDraw.Draw(lay)
    x0, y0, x1, y1 = BOT
    s = c.speaking(T)
    if s:
        col = c.col[s["speaker"]]
        d.ellipse((x0 + 22, y0 + 22, x0 + 38, y0 + 38), fill=col)
        d.text((x0 + 48, y0 + 14), c.labels[s["speaker"]], font=font(22, "Heavy"), fill=col)
        words, times = s["text"].split(), s.get("words") or []
        f = font(36, "Semibold")
        lines = wrap_words(words, f, 1040)
        k = -1
        for j, tt in enumerate(times):
            if tt and tt[0] <= T:
                k = j
        if T > s["end"]:
            k = len(words) - 1
        li = next((n for n, ln in enumerate(lines) if k in ln), 0)
        yy = y0 + 52
        for ln in lines[max(0, li - 1): max(0, li - 1) + 3]:
            xx = x0 + 22
            for j in ln:
                if j == k:
                    rrect(d, (xx - 6, yy + 4, xx + text_w(words[j], f) + 6, yy + 48), 8, fill=col + (70,))
                d.text((xx, yy), words[j] + " ", font=f, fill=TEXT if j <= k else (88, 95, 112))
                xx += text_w(words[j] + " ", f)
            yy += 48
    ls = c.last_scored(T)
    bx = x0 + 1130
    if ls:
        col = c.col[ls["speaker"]]
        age = T - ls["end"]
        d.text((bx, y0 + 16), "JEV", font=font(19, "Heavy"), fill=JEV)
        d.text((bx + 44, y0 + 16), f"scored last sentence in {ls['ms']} ms", font=font(19, "Medium"), fill=DIM)
        step = min(41, int(200 / max(1, len(c.qs))))
        for n, (k, label) in enumerate(c.qs):
            yy = y0 + 52 + n * step
            v = ls["scores"][k] * ease(clamp((age - n * 0.05) / 0.35))
            d.text((bx, yy), label, font=font(20, "Medium"), fill=(205, 210, 220))
            bx0, bx1 = bx + 190, x1 - 92
            rrect(d, (bx0, yy + 8, bx1, yy + 22), 7, fill=(32, 37, 50))
            hot = ls["scores"][k] >= c.thr[k]
            cc = NEUTRAL if k in c.qneutral else col
            if v > 0.005:
                L = max(14, (bx1 - bx0) * v)
                rrect(d, (bx0, yy + 8, bx0 + L, yy + 22), 7, fill=mix(cc, (30, 34, 46), 0.0 if hot else 0.4))
                d.ellipse((bx0 + L - 11, yy + 4, bx0 + L + 11, yy + 26), fill=cc)
            pv = f"{v:.2f}"
            d.text((x1 - 22 - text_w(pv, font(22, "Bold", True)), yy - 1), pv, font=font(22, "Bold", True), fill=cc if hot else TEXT)
    comp(img, lay)


def draw_beam(c, img, T):
    ls = c.last_scored(T)
    if not ls:
        return
    age = T - ls["end"]
    if not 0 <= age < 0.45:
        return
    y = FY - 140 + (FH + 140) * ease(clamp(age / 0.45))
    top, bot = max(FY, y), min(FY + FH, y + 140)
    if bot - top <= 2:
        return
    lay = layer()
    lay.paste(c.beam.crop((0, int(top - y), FW, int(top - y) + int(bot - top))), (FX, int(top)))
    comp(img, lay, 1 - clamp((age - 0.3) / 0.15))


def draw_streak(c, img, T):
    ls = c.last_scored(T)
    if not ls:
        return
    age = T - ls["end"]
    if not 0.15 <= age < 0.75:
        return
    p = ease(clamp((age - 0.15) / 0.55))
    sx, sy = BOT[0] + 1530, BOT[1] + 60
    x0, y0, x1, _ = LP if c.side[ls["speaker"]] == "L" else RP
    ex, ey = (x0 + x1) / 2, y0 + 225
    cx, cy = (sx + ex) / 2, min(sy, ey) - 160
    lay = layer()
    d = ImageDraw.Draw(lay)
    col = c.col[ls["speaker"]]
    for j in range(14):
        q = clamp(p - j * 0.025)
        bx = (1 - q) ** 2 * sx + 2 * (1 - q) * q * cx + q * q * ex
        by = (1 - q) ** 2 * sy + 2 * (1 - q) * q * cy + q * q * ey
        r = 9 - j * 0.55
        d.ellipse((bx - r, by - r, bx + r, by + r), fill=mix(JEV, col, j / 14) + (int(255 * (1 - j / 14)),))
    comp(img, lay, 1 - clamp((p - 0.9) / 0.1))


def draw_hook(c, img, t, text):
    out = clamp((4.6 - t) / 0.45)
    if out <= 0 or not text:
        return
    lay = layer()
    sc = np.zeros((FH, FW, 4), np.uint8)
    sc[..., 3] = (np.linspace(0, 1, FH) ** 1.2 * 230).astype(np.uint8)[:, None]
    lay.paste(Image.fromarray(sc), (FX, FY), c.mask)
    d = ImageDraw.Draw(lay)
    words_all = text.split()
    f1 = font(70, "Heavy")
    lines = wrap_words(words_all, f1, FW - 120)[:3]
    if len(lines) == 2:  # balance two lines so the last one is never a lonely word
        best = min(range(1, len(words_all)), key=lambda i: max(text_w(" ".join(words_all[:i]), f1), text_w(" ".join(words_all[i:]), f1)))
        if max(text_w(" ".join(words_all[:best]), f1), text_w(" ".join(words_all[best:]), f1)) <= FW - 120:
            lines = [list(range(best)), list(range(best, len(words_all)))]
    base_y = FY + FH - 190 - 82 * len(lines)
    wi = 0
    for li, ln in enumerate(lines):
        line = " ".join(words_all[j] for j in ln)
        colr = JEV if li == len(lines) - 1 and len(lines) > 1 else (255, 255, 255)
        x = FX + FW / 2 - text_w(line, f1) / 2
        y = base_y + li * 82
        for j in ln:
            a = ease(clamp((t - wi * 0.09) / 0.22))
            if a > 0:
                d.text((x, y + (1 - a) * 22), words_all[j], font=f1, fill=colr + (int(255 * a),))
            x += text_w(words_all[j] + " ", f1)
            wi += 1
        if li == len(lines) - 1:
            u = ease(clamp((t - 0.9) / 0.4))
            lw = text_w(line, f1)
            ux = FX + FW / 2 - lw / 2
            if u > 0:
                rrect(d, (ux, y + 84, ux + lw * u, y + 92), 4, fill=JEV + (255,))
    sub = c.preset.get("hook_sub", "")
    if sub:
        sa = ease(clamp((t - 1.2) / 0.3))
        d.text((FX + FW / 2 - text_w(sub, font(26, "Medium")) / 2, FY + FH - 160), sub, font=font(26, "Medium"), fill=(215, 219, 228, int(255 * sa)))
    comp(img, lay, out)


# ---------------------------------------------------------------- frames
def update_disp(c, sm, st_i, st, tok, rate=0.2):
    disp = {"calls": sm.go("calls", st_i, 0.35), "tok": sm.go("tok", tok, 0.35)}
    for sp in c.speakers:
        disp[f"{sp}:idx"] = sm.go(f"{sp}:idx", st[sp][1]["idx"], rate)
        for k, _ in c.qs:
            disp[f"{sp}:{k}"] = sm.go(f"{sp}:{k}", st[sp][1][k], rate)
    return disp


def base(c, act):
    img = c.bg.copy()
    for sp in c.speakers:
        comp(img, c.amb[sp], (0.25 + 0.75 * act.get(sp, 0)) * 0.8)
    return img


def panels(c, img, T, st, act, disp, live=True):
    for sp in c.speakers:
        ld = c.last_scored(T, sp)
        flash = clamp(1 - (T - ld["end"]) / 0.5) if (ld and live) else 0
        draw_panel(c, img, sp, st[sp], act.get(sp, 0), flash, c.flags(T, sp) if live else [], disp)
    if c.single:
        draw_feed(c, img, T)


def frame_clip(c, seg, k, clip, sm, first, n):
    T = seg["src"] + k / FPS
    st_i, st, tok = c.stats(T)
    s = c.speaking(T)
    act = {sp: sm.go(f"act:{sp}", 1.0 if (s and s["speaker"] == sp) else 0.0, 0.2) for sp in c.speakers}
    disp = update_disp(c, sm, st_i, st, tok)
    img = base(c, act)
    fr = clip.next()
    split = len(c.speakers) == 2 and is_split(fr)
    tx, tz = 0.5, 1.04
    if split and s:
        tx, tz = (0.40 if c.side[s["speaker"]] == "L" else 0.60), 1.10
    age = c.recent_flag_age(T)
    kick = 0.05 * math.exp(-age * 6) * clamp(age / 0.05) if age is not None else 0.0
    if k == 0:
        sm.v["cx"], sm.v["z"] = tx, tz + 0.06
    cx = sm.go("cx", tx, 0.06)
    z = sm.go("z", tz, 0.06) + kick + 0.02 * k / max(1, n)
    blur = 0
    if not first and not seg.get("no_whip") and k < 6:
        blur = (6 - k) * 7
    elif k > n - 4 and not seg.get("no_tail"):
        blur = (k - (n - 4)) * 9
    img.paste(grade(camera(fr, z, cx + 0.004 * math.sin(k / 45), 0.5, blur)), (FX, FY), c.mask)
    vg = layer()
    vg.paste(c.vig, (FX, FY), c.mask)
    img.alpha_composite(vg)
    if s:
        comp(img, c.foot_glow[s["speaker"]], 0.55 * act[s["speaker"]])
    ImageDraw.Draw(img).rounded_rectangle((FX, FY, FX + FW - 1, FY + FH - 1), 18, outline=(50, 56, 72), width=2)
    draw_beam(c, img, T)
    if k < FPS * 2.2 and not seg.get("hook") and not seg.get("no_whip"):
        mm, ss = divmod(int(T), 60)
        hh, mm = divmod(mm, 60)
        lab = f"{hh}:{mm:02d}:{ss:02d} into the video"
        f = font(20, "Semibold", True)
        lay = layer()
        ld = ImageDraw.Draw(lay)
        rrect(ld, (FX + 18, FY + 18, FX + 42 + text_w(lab, f), FY + 54), 10, fill=(0, 0, 0, 170))
        ld.text((FX + 30, FY + 24), lab, font=f, fill=(230, 232, 238))
        comp(img, lay, clamp(k / 5) * clamp((FPS * 2.2 - k) / 8))
    panels(c, img, T, st, act, disp)
    draw_header(c, img, T, disp)
    draw_bottom(c, img, T)
    draw_streak(c, img, T)
    if seg.get("hook"):
        draw_hook(c, img, k / FPS, c.cfg.get("hook"))
    return img


def frame_hyper(c, seg, k, sm, frames):
    n = int(round(seg["dur"] * FPS))
    pe = (k / max(1, n - 1)) ** 1.15
    T = seg["from"] + (seg["to"] - seg["from"]) * pe
    st_i, st, tok = c.stats(T)
    disp = update_disp(c, sm, st_i, st, tok, 0.5)
    act = {sp: 0.4 for sp in c.speakers}
    img = base(c, act)
    foot = grade(frames(T).resize((FW, FH), Image.BICUBIC, box=(DW * 0.03, DH * 0.03, DW * 0.97, DH * 0.97)))
    img.paste(Image.blend(foot, Image.new("RGB", (FW, FH)), 0.2), (FX, FY), c.mask)
    vg = layer()
    vg.paste(c.vig, (FX, FY), c.mask)
    img.alpha_composite(vg)
    lay = layer()
    d = ImageDraw.Draw(lay)
    rrect(d, (FX + 30, FY + FH - 34, FX + FW - 30, FY + FH - 26), 4, fill=(255, 255, 255, 40))
    rrect(d, (FX + 30, FY + FH - 34, FX + 30 + (FW - 60) * pe, FY + FH - 26), 4, fill=JEV + (255,))
    el = T - seg["from"]
    long_video = seg["to"] - seg["from"] >= 600
    lab = f"{int(el // 60)}" if long_video else f"{int(el // 60)}:{int(el % 60):02d}"
    unit = "min" if long_video else ""
    f = font(150, "Heavy", True)
    tw = text_w(lab, f) + (text_w(unit, font(54, "Bold")) + 20 if unit else 0)
    d.text((FX + FW / 2 - tw / 2, FY + FH / 2 - 150), lab, font=f, fill=(255, 255, 255, 255))
    if unit:
        d.text((FX + FW / 2 + tw / 2 - text_w(unit, font(54, "Bold")), FY + FH / 2 - 42), unit, font=font(54, "Bold"), fill=JEV + (255,))
    sub = "the whole video · every sentence through Jev"
    d.text((FX + FW / 2 - text_w(sub, font(28, "Semibold")) / 2, FY + FH / 2 + 30), sub, font=font(28, "Semibold"), fill=(225, 228, 236, 255))
    fade = clamp(k / 6) * clamp((n - k) / 6)
    comp(img, lay.filter(ImageFilter.GaussianBlur(10)), 0.8 * fade)
    comp(img, lay, fade)
    panels(c, img, T, st, act, disp, live=False)
    draw_header(c, img, T, disp)
    lay = layer()
    d = ImageDraw.Draw(lay)
    x0, y0, x1, y1 = BOT
    d.text((x0 + 22, y0 + 14), f"{c.index_label} over the video", font=font(21, "Bold"), fill=(215, 219, 228))
    d.text((x0 + 22 + text_w(f"{c.index_label} over the video", font(21, "Bold")) + 16, y0 + 16), "running average of Jev probabilities",
           font=font(19, "Medium"), fill=DIM)
    cx0, cx1, cy0, cy1 = x0 + 22, x1 - 70, y0 + 56, y1 - 22
    lo, hi = seg["chart_lo"], seg["chart_hi"]
    for g in (lo, (lo + hi) / 2, hi):
        yy = cy1 - (g - lo) / (hi - lo) * (cy1 - cy0)
        d.line((cx0, yy, cx1, yy), fill=(255, 255, 255, 22))
        d.text((cx1 + 14, yy - 11), f"{g * 100:.0f}", font=font(17, "Medium", True), fill=DIM)
    glow = layer()
    gd = ImageDraw.Draw(glow)
    for sp in c.speakers:
        pts = []
        for q in range(241):
            tq = seg["from"] + (seg["to"] - seg["from"]) * q / 240
            if tq > T:
                break
            n_, avg = c.stats(tq)[1][sp]
            if n_ >= 8:
                pts.append((cx0 + (cx1 - cx0) * q / 240, cy1 - clamp((avg["idx"] - lo) / (hi - lo)) * (cy1 - cy0)))
        if len(pts) > 1:
            d.polygon(pts + [(pts[-1][0], cy1), (pts[0][0], cy1)], fill=c.col[sp] + (26,))
            d.line(pts, fill=c.col[sp] + (255,), width=5, joint="curve")
            hx, hy = pts[-1]
            gd.ellipse((hx - 22, hy - 22, hx + 22, hy + 22), fill=c.col[sp] + (200,))
            d.ellipse((hx - 8, hy - 8, hx + 8, hy + 8), fill=(255, 255, 255, 255))
    comp(img, glow.filter(ImageFilter.GaussianBlur(10)))
    comp(img, lay)
    return img


def frame_end(c, seg, k, final, sm):
    st_i, st, tok = c.stats(1e12)
    disp = update_disp(c, sm, st_i, st, tok, 1.0)
    act = {sp: 0.45 for sp in c.speakers}
    img = base(c, act)
    z = 1.06 + 0.04 * k / (seg["dur"] * FPS)
    fr = final.resize((FW, FH), Image.BICUBIC, box=(DW / 2 - DW / z / 2, DH / 2 - DH / z / 2, DW / 2 + DW / z / 2, DH / 2 + DH / z / 2))
    fr = fr.filter(ImageFilter.GaussianBlur(ease(clamp(k / 20)) * 8))
    img.paste(Image.blend(fr, Image.new("RGB", (FW, FH), (4, 6, 10)), 0.35 + 0.4 * ease(clamp(k / 20))), (FX, FY), c.mask)
    panels(c, img, 1e12, st, act, disp, live=False)
    draw_header(c, img, 1e12, disp)
    lay = layer()
    d = ImageDraw.Draw(lay)
    a = ease(clamp(k / 12))
    t1 = c.preset.get("end_title", "Final score: the whole video")
    d.text((FX + FW / 2 - text_w(t1, font(50, "Heavy")) / 2, FY + 30 + (1 - a) * 20), t1, font=font(50, "Heavy"), fill=(255, 255, 255, int(255 * a)))
    t2 = "average Jev probability per sentence"
    d.text((FX + FW / 2 - text_w(t2, font(24, "Medium")) / 2, FY + 92), t2, font=font(24, "Medium"), fill=(200, 205, 215, int(255 * a)))
    rows = [("idx", c.index_label)] + c.qs
    mid = FX + FW / 2
    y = FY + 160
    glow = layer()
    gd = ImageDraw.Draw(glow)
    rh = min(68, int(440 / max(1, len(rows) - 1)))
    for r, (key, name) in enumerate(rows):
        pr = ease(clamp((k - 8 - r * 5) / 18))
        big = r == 0
        nf = font(26 if big else 22, "Heavy" if big else "Semibold")
        d.text((mid - text_w(name.upper(), nf) / 2, y), name.upper(), font=nf, fill=(230, 232, 238, int(255 * clamp(pr * 2))))
        yb, hb = y + (36 if big else 32), (30 if big else 20)
        for i, sp in enumerate(c.speakers):
            sign = -1 if (c.side[sp] == "L" and not c.single) else 1
            v = st[sp][1][key]
            L = (470 if not c.single else 560) * v * pr
            box = (mid - 20 - L, yb, mid - 20, yb + hb) if sign < 0 else (mid + 20 - (0 if not c.single else 290), yb, mid + 20 - (0 if not c.single else 290) + L, yb + hb)
            if L > 1:
                rrect(d, box, hb // 2, fill=c.col[sp] + (255,))
                rrect(gd, box, hb // 2, fill=c.col[sp] + (170,))
            pv = f"{v * 100 * pr:.0f}"
            nf2 = font(30 if big else 24, "Bold", True)
            tx = box[0] - text_w(pv, nf2) - 18 if sign < 0 else box[2] + 18
            if pr > 0:
                d.text((tx, yb - (6 if big else 4)), pv, font=nf2, fill=c.col[sp] + (255,))
        y += (80 + 18) if big else rh
    comp(img, glow.filter(ImageFilter.GaussianBlur(12)), 0.8)
    x0, y0, x1, y1 = BOT
    items = [(f"{st_i:,}", "sentences scored"), (f"{st_i * len(c.qs):,}", "yes/no answers"), (f"{tok:,}", "input tokens"),
             (f"${tok / 1e6 * c.price:.3f}", "total cost"), (f"{c.summary['median_ms'] / 1000:.1f} s", "median per call")]
    cw = (x1 - x0) / len(items)
    for n_, (v, l) in enumerate(items):
        pr = ease(clamp((k - 30 - n_ * 4) / 14))
        cx = x0 + cw * n_ + cw / 2
        f = font(58, "Heavy", True)
        d.text((cx - text_w(v, f) / 2, y0 + 44 + (1 - pr) * 20), v, font=f, fill=(JEV if "$" in v else (255, 255, 255)) + (int(255 * pr),))
        d.text((cx - text_w(l, font(22, "Medium")) / 2, y0 + 122 + (1 - pr) * 20), l, font=font(22, "Medium"), fill=DIM + (int(255 * pr),))
    idx_names = ", ".join(lbl.lower() for key, lbl in c.qs if key in c.idx)
    foot = c.preset.get("disclaimer", f"Scores are Jev model probabilities, not a fact-check. {c.index_label} = mean of {idx_names}.")
    d.text((W / 2 - text_w(foot, font(19, "Medium")) / 2, y1 - 44), foot, font=font(19, "Medium"), fill=(150, 156, 170, int(255 * ease(clamp((k - 50) / 15)))))
    comp(img, lay)
    return img


def render(work, out, lo, hi, stills=None):
    c = Ctx(work)
    edl = json.load(open(os.path.join(work, "edl.json")))
    cache = os.path.join(work, "cache")
    enc = None
    if stills is None:
        enc = subprocess.Popen([ffmpeg(), "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                                "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
                                "-movflags", "+faststart", out], stdin=subprocess.PIPE)
    frames_cache = {}

    def frames(T):
        key = int(T // 20) * 20 + 5
        if key not in frames_cache:
            frames_cache[key] = still(c.video, min(key, c.cfg.get("video_duration", 1e12) - 2), cache)
        return frames_cache[key]

    g = sum(int(round(s["dur"] * FPS)) for s in edl[:lo])
    for si in range(lo, hi):
        seg = edl[si]
        n = int(round(seg["dur"] * FPS))
        want = set(range(n)) if stills is None else {x - g for x in stills if g <= x < g + n}
        if not want:
            g += n
            continue
        sm = Smooth()
        clip = Clip(c.video, seg["src"], seg["dur"]) if seg["type"] == "clip" else None
        final = still(c.video, seg.get("still", 0), cache) if seg["type"] == "end" else None
        for k in range(n):
            if k > max(want):
                break
            if seg["type"] == "clip":
                img = frame_clip(c, seg, k, clip, sm, si == 0, n)
            elif seg["type"] == "hyper":
                img = frame_hyper(c, seg, k, sm, frames)
            else:
                img = frame_end(c, seg, k, final, sm)
            if k not in want:
                continue
            if stills is not None:
                img.convert("RGB").save(os.path.join(work, f"still_{g + k:05d}.png"))
            else:
                enc.stdin.write(img.convert("RGB").tobytes())
        if clip:
            clip.close()
        g += n
    if enc:
        enc.stdin.close()
        enc.wait()


if __name__ == "__main__":
    work, out, lo, hi = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    stills = {int(x) for x in sys.argv[5].split(",")} if len(sys.argv) > 5 else None
    render(work, out, lo, hi, stills)
