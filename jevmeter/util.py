"""Shared helpers: ffmpeg location, fonts, logging, small math."""
import os
import shutil
import subprocess
import sys

import numpy as np
from PIL import ImageFont


def log(*a):
    print("[jevmeter]", *a, file=sys.stderr, flush=True)


def ffmpeg():
    """Prefer a system ffmpeg, fall back to the binary bundled with imageio-ffmpeg."""
    exe = os.environ.get("JEVMETER_FFMPEG") or shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit("ffmpeg not found: install ffmpeg or `pip install imageio-ffmpeg`")


def probe_duration(path):
    out = subprocess.run([ffmpeg(), "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            h, m, s = line.split(",")[0].split("Duration:")[1].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise RuntimeError(f"could not read duration of {path}")


def load_audio(path, sr=16000, start=None, dur=None):
    cmd = [ffmpeg(), "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, np.float32)


# ---------------------------------------------------------------- fonts
_SANS = [
    ("/System/Library/Fonts/SFNS.ttf", True),                        # macOS, variable
    ("/Library/Fonts/Inter-Variable.ttf", True),
    (os.path.expanduser("~/Library/Fonts/InterVariable.ttf"), True),
    ("/usr/share/fonts/truetype/inter/InterVariable.ttf", True),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", False),
    ("C:/Windows/Fonts/segoeui.ttf", False),
    ("C:/Windows/Fonts/arial.ttf", False),
]
_SANS_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"]
_MONO = ["/System/Library/Fonts/SFNSMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf"]
_MONO_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "C:/Windows/Fonts/consolab.ttf"]
_BOLD = {"Semibold", "Bold", "Heavy", "Black"}
_cache = {}


def _first(paths):
    return next((p for p in paths if os.path.exists(p)), None)


def font(size, weight="Regular", mono=False):
    """Weighted font with graceful fallbacks; override with JEVMETER_FONT / JEVMETER_FONT_MONO."""
    key = (size, weight, mono)
    if key in _cache:
        return _cache[key]
    f = None
    env = os.environ.get("JEVMETER_FONT_MONO" if mono else "JEVMETER_FONT")
    candidates = [(env, True)] if env else []
    if mono:
        candidates += [(p, True) for p in (_MONO if weight not in _BOLD else [_first(_MONO_BOLD) or _first(_MONO)]) if p]
    else:
        if weight in _BOLD and _first(_SANS_BOLD) and not os.path.exists(_SANS[0][0]):
            candidates += [(_first(_SANS_BOLD), False)]
        candidates += _SANS
    for path, variable in candidates:
        if path and os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                if variable:
                    try:
                        f.set_variation_by_name(weight)
                    except Exception:
                        pass
                break
            except Exception:
                continue
    if f is None:
        f = ImageFont.load_default(size)
    _cache[key] = f
    return f


def ease(p):
    return p * p * (3 - 2 * p)


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
