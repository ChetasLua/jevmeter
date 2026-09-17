"""Soundtrack: the video's own audio under clips, a riser + ticks under the hyperlapse, whooshes between clips,
soft blips on flags, a low hit under the end card. Everything is synthesized, so no music licensing."""
import json
import os
import subprocess
import wave

import numpy as np

from .util import ffmpeg

SR, FPS = 48000, 30


def _src(video, t, d):
    b = subprocess.run([ffmpeg(), "-loglevel", "error", "-ss", f"{t:.3f}", "-i", video, "-t", f"{d:.3f}", "-vn", "-ac", "2", "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True).stdout
    a = np.frombuffer(b, np.float32).reshape(-1, 2)
    n = int(d * SR)
    return np.pad(a, ((0, max(0, n - len(a))), (0, 0)))[:n]


def build(work, out_wav):
    cfg = json.load(open(os.path.join(work, "render_config.json")))
    edl = json.load(open(os.path.join(work, "edl.json")))
    tl = json.load(open(os.path.join(work, "timeline.json")))
    ends = sorted(s["end"] for s in tl["sentences"])
    thr = tl["summary"]["thresholds"]
    flagged = [s["end"] for s in tl["sentences"] if any(s["scores"][k] >= thr[k] for k in thr)]
    total = sum(int(round(s["dur"] * FPS)) for s in edl) / FPS
    mix = np.zeros((int(total * SR) + SR, 2), np.float32)
    rng = np.random.default_rng(7)
    t = 0.0
    for seg in edl:
        d = int(round(seg["dur"] * FPS)) / FPS
        i0, n = int(t * SR), int(d * SR)
        x = np.arange(n) / SR
        if seg["type"] == "clip":
            a = _src(cfg["video"], seg["src"], d)
            env = np.ones(n, np.float32)
            fi = 0 if seg.get("no_whip") else int((0.004 if t == 0 else 0.03) * SR)
            fo = 0 if seg.get("no_tail") else int(0.06 * SR)
            if fi:
                env[:fi] = np.linspace(0, 1, fi)
            if fo:
                env[n - fo:] = np.minimum(env[n - fo:], np.linspace(1, 0, fo))
            mix[i0:i0 + n] += a * env[:, None]
            if t > 0 and not seg.get("no_whip"):
                m = int(0.45 * SR)
                nz = np.convolve(rng.standard_normal(m).astype(np.float32), np.ones(24) / 24, mode="same")
                w = 0.5 * nz * np.sin(np.pi * np.arange(m) / m) ** 2
                j = max(0, i0 - int(0.2 * SR))
                mix[j:j + m] += w[:, None]
            for e in flagged:
                if seg["src"] <= e < seg["src"] + d:
                    j, m = i0 + int((e - seg["src"]) * SR), int(0.12 * SR)
                    xx = np.arange(m) / SR
                    blip = 0.10 * (np.sin(2 * np.pi * 880 * xx) + 0.5 * np.sin(2 * np.pi * 1320 * xx)) * np.exp(-xx * 30)
                    mix[j:j + m] += blip[:len(mix[j:j + m])][:, None]
        elif seg["type"] == "hyper":
            bed = rng.standard_normal(n).astype(np.float32) * 0.02 * (x / d) ** 2 + 0.05 * np.sin(2 * np.pi * 55 * x) * np.minimum(1, x / 0.5) * np.minimum(1, (d - x) / 0.4)
            mix[i0:i0 + n] += bed[:, None]
            step = max(1, len(ends) // 60)
            prev, nf = 0, int(d * FPS)
            for k in range(nf):
                T = seg["from"] + (seg["to"] - seg["from"]) * (k / max(1, nf - 1)) ** 1.15
                cnt = int(np.searchsorted(ends, T, side="right")) // step
                if cnt > prev:
                    j, m = i0 + int(k / FPS * SR), int(0.03 * SR)
                    tt = np.arange(m) / SR
                    tick = 0.16 * np.sin(2 * np.pi * (1800 + cnt * 6) * tt) * np.exp(-tt * 140)
                    mix[j:j + m] += tick[:len(mix[j:j + m])][:, None]
                prev = cnt
        else:
            s = 0.45 * np.sin(2 * np.pi * (48 + 30 * np.exp(-x * 9)) * x) * np.exp(-x * 2.2)
            s += 0.035 * (np.sin(2 * np.pi * 110 * x) + 0.6 * np.sin(2 * np.pi * 164.8 * x) + 0.4 * np.sin(2 * np.pi * 220 * x)) * np.minimum(1, x / 1.5) * np.minimum(1, (d - x) / 1.5)
            mix[i0:i0 + n] += s[:, None]
        t += d
    mix = mix[:int(total * SR)]
    peak = float(np.abs(mix).max()) or 1.0
    mix *= min(1.0, 0.97 / peak)
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix * 32767).astype(np.int16).tobytes())
