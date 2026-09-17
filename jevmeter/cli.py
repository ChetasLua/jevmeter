"""jevmeter: put a live Jev (TypeSafe System One) meter on any video.

    export TYPESAFE_API_KEY=...
    jevmeter run debate.mp4 --transcript transcript.txt --speakers "TRUMP,HARRIS" --preset debate
    jevmeter run podcast.mp4 --preset podcast --mode full --start 600 --end 780
"""
import argparse
import json
import os
import subprocess
import sys
import time
from importlib import resources

from . import __version__
from .util import ffmpeg, log, probe_duration


def load_preset(name):
    if os.path.exists(name):
        return json.load(open(name))
    try:
        return json.loads(resources.files("jevmeter.presets").joinpath(f"{name}.json").read_text())
    except FileNotFoundError:
        names = sorted(p.name[:-5] for p in resources.files("jevmeter.presets").iterdir() if p.name.endswith(".json"))
        raise SystemExit(f"unknown preset {name!r}; built-in: {', '.join(names)} (or pass a path to a JSON file)")


def chart_range(tl):
    import statistics as st
    idx = tl["summary"]["index_keys"]
    vals = []
    for sp in tl["speakers"]:
        rs = [r for r in tl["sentences"] if r["speaker"] == sp]
        acc = 0.0
        for n, r in enumerate(rs, 1):
            acc += sum(r["scores"][k] for k in idx) / max(1, len(idx))
            if n >= 8:
                vals.append(acc / n)
    if not vals:
        return 0.0, 1.0
    lo, hi = min(vals), max(vals)
    pad = max(0.03, (hi - lo) * 0.25)
    lo, hi = max(0.0, lo - pad), min(1.0, hi + pad)
    return round(lo, 2), round(hi, 2)


def fetch_if_url(video):
    """Allow a link instead of a file (needs `pip install yt-dlp`). Only use videos you have the right to use."""
    if not video.startswith(("http://", "https://")):
        return os.path.expanduser(video)
    try:
        import yt_dlp
    except ImportError:
        sys.exit("To use a link, install yt-dlp first:  pip install yt-dlp   (or download the video and pass the file)")
    out_dir = os.path.abspath("jevmeter-downloads")
    os.makedirs(out_dir, exist_ok=True)
    opts = {"outtmpl": os.path.join(out_dir, "%(title).60s [%(id)s].%(ext)s"), "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
            "merge_output_format": "mp4", "ffmpeg_location": ffmpeg(), "quiet": True, "noprogress": False}
    log(f"downloading {video}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video, download=True)
        path = ydl.prepare_filename(info)
    base = os.path.splitext(path)[0] + ".mp4"
    return base if os.path.exists(base) else path


def cmd_run(a):
    from . import audio, plan, score, transcribe

    from .config import get_key
    key = get_key()
    if not key:
        sys.exit("No TypeSafe API key found. Run `jevmeter setup` (or set TYPESAFE_API_KEY). Get a key at https://console.typesafe.ai/keys")
    video = fetch_if_url(a.video)
    video = os.path.abspath(video)
    if not os.path.exists(video):
        sys.exit(f"no such video: {video}")
    work = os.path.abspath(a.work or os.path.splitext(video)[0] + ".jevmeter")
    os.makedirs(work, exist_ok=True)
    preset = load_preset(a.preset)
    t0 = time.time()

    sents = transcribe.build_sentences(video, work, a.transcript, a.whisper_model, a.language, a.start, a.end,
                                       speaker_name=(a.speakers.split(",")[0] if (a.speakers and not a.transcript) else None))
    speakers = plan.pick_speakers(sents, a.speakers.split(",") if (a.speakers and a.transcript) else None)
    log(f"{len(sents)} sentences; scoring speakers: {', '.join(speakers)}")
    scores = score.score(sents, speakers, preset, work, key, model=a.model, threads=a.threads)
    tl = plan.build_timeline(sents, scores, speakers, preset, work)
    summ = tl["summary"]
    cost = summ["input_tokens"] / 1e6 * a.price
    log(f"Jev done: {summ['sentences']} sentences, {summ['input_tokens']:,} input tokens, ${cost:.4f}, median {summ['median_ms']} ms")
    for sp, v in summ["speakers"].items():
        log(f"  {sp}: index {v['index']*100:.1f} " + " ".join(f"{q['key']}={v[q['key']]:.2f}" for q in preset["questions"]))
    if a.score_only:
        print(json.dumps(summ, indent=2))
        return

    vdur = probe_duration(video)
    if a.mode == "full":
        edl = plan.plan_full(tl, a.start, a.end, vdur)
    else:
        edl = plan.plan_highlights(tl, preset, clips_per_speaker=a.clips, first_after=(a.start or 0))
        lo, hi = chart_range(tl)
        for seg in edl:
            if seg["type"] == "hyper":
                seg["chart_lo"], seg["chart_hi"] = lo, hi
    json.dump(edl, open(os.path.join(work, "edl.json"), "w"), indent=1)
    labels = dict(kv.split("=", 1) for kv in a.label) if a.label else {}
    json.dump({"video": video, "video_duration": vdur, "preset": preset, "hook": a.hook if a.hook is not None else preset.get("hook", ""),
               "colors": a.colors.split(",") if a.colors else None, "labels": {k.upper(): v for k, v in labels.items()},
               "price_per_million_input": a.price}, open(os.path.join(work, "render_config.json"), "w"), indent=1)

    out = os.path.abspath(a.out or os.path.splitext(video)[0] + ".jevmeter.mp4")
    render_parts(work, edl, a.workers)
    wav = os.path.join(work, "audio.wav")
    audio.build(work, wav)
    parts = sorted(p for p in os.listdir(work) if p.startswith("part_") and p.endswith(".mp4"))
    with open(os.path.join(work, "parts.txt"), "w") as f:
        f.writelines(f"file '{os.path.join(work, p)}'\n" for p in parts)
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "parts.txt"), "-i", wav,
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
                    "-shortest", "-movflags", "+faststart", out], check=True)
    log(f"wrote {out}  ({time.time() - t0:.0f}s total)")


def render_parts(work, edl, workers):
    for p in os.listdir(work):
        if p.startswith("part_"):
            os.remove(os.path.join(work, p))
    n = len(edl)
    frames = [s["dur"] for s in edl]
    if n == 1 and edl[0]["type"] == "clip" and workers > 1:
        # one long clip: split it into equal time slices so workers can run in parallel
        seg = edl[0]
        step = seg["dur"] / workers
        sub = []
        for i in range(workers):
            s = {"type": "clip", "src": round(seg["src"] + i * step, 3), "dur": round(step, 3)}
            if i == 0:
                s["hook"] = seg.get("hook", False)
            else:
                s["no_whip"] = True
            if i < workers - 1:
                s["no_tail"] = True
            sub.append(s)
        edl[:] = sub + edl[1:]
        json.dump(edl, open(os.path.join(work, "edl.json"), "w"), indent=1)
        n, frames = len(edl), [s["dur"] for s in edl]
    # balance segments across workers by duration
    groups, cur, acc, target = [], [], 0.0, sum(frames) / max(1, workers)
    for i in range(n):
        cur.append(i)
        acc += frames[i]
        if acc >= target and len(groups) < workers - 1:
            groups.append(cur)
            cur, acc = [], 0.0
    if cur:
        groups.append(cur)
    log(f"rendering {sum(frames):.0f}s of video in {len(groups)} parallel parts")
    procs = []
    for gi, g in enumerate(groups):
        out = os.path.join(work, f"part_{gi:02d}.mp4")
        procs.append(subprocess.Popen([sys.executable, "-m", "jevmeter.render", work, out, str(g[0]), str(g[-1] + 1)]))
    codes = [p.wait() for p in procs]
    if any(codes):
        sys.exit(f"render failed (exit codes {codes})")


PRESET_BLURBS = [
    ("debate", "🦉", "Debates & interviews", "BS index: evasive, dodged the question, emotional appeal, contradicts self"),
    ("earnings_call", "🦊", "Earnings calls & investor Q&A", "Spin index: vague guidance, blaming outside factors, hype, dodges"),
    ("podcast", "🐉", "Podcasts & YouTube talk", "Hot take index: unsupported claims, overgeneralizations, outrage, self-promotion"),
    ("sales_pitch", "🦚", "Pitches, launches & ads", "Hype index: buzzwords, overpromises, urgency pressure, vague benefits"),
]


def _ask(prompt, default=None):
    tip = f" [{default}]" if default not in (None, "") else ""
    try:
        v = input(f"{prompt}{tip}: ").strip()
    except EOFError:
        v = ""
    return v or (default if default is not None else "")


def _clean_path(p):
    # terminals wrap dragged-in paths in quotes or escape spaces
    p = p.strip().strip("'").strip('"').replace("\\ ", " ")
    return os.path.expanduser(p)


def cmd_setup(a=None):
    import getpass
    from .config import check_key, get_key, mask, save_key
    print("\n🔑  TypeSafe API key")
    print("    Create one (free to start) at https://console.typesafe.ai/keys")
    cur = get_key()
    if cur:
        print(f"    Current key: {mask(cur)}")
    key = getpass.getpass("    Paste your key (hidden, press Enter to keep the current one): ").strip()
    if not key:
        if not cur:
            sys.exit("    No key entered.")
        key = cur
    ok, msg = check_key(key)
    print(("    ✅ " if ok else "    ❌ ") + msg)
    if not ok:
        sys.exit(1)
    path = save_key(key)
    print(f"    Saved to {path} (only your user can read it; it is never stored in the project folder).\n")
    return key


def cmd_doctor(a=None):
    import importlib.util
    from .config import check_key, get_key, mask
    good = True
    def line(ok, text):
        nonlocal good
        good &= ok
        print(("  ✅ " if ok else "  ❌ ") + text)
    print("\njevmeter doctor")
    line(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}")
    try:
        line(True, f"ffmpeg: {ffmpeg()}")
    except SystemExit:
        line(False, "ffmpeg missing: pip install imageio-ffmpeg")
    backend = "mlx-whisper" if importlib.util.find_spec("mlx_whisper") else ("faster-whisper" if importlib.util.find_spec("faster_whisper") else None)
    line(bool(backend), f"speech-to-text: {backend}" if backend else "speech-to-text missing: pip install 'jevmeter[mlx]' (Apple Silicon) or 'jevmeter[cpu]'")
    key = get_key()
    if not key:
        line(False, "TypeSafe API key: not set, run `jevmeter setup`")
    else:
        ok, msg = check_key(key)
        line(ok, f"TypeSafe API key {mask(key)}: {msg}")
    print("\n  All good, run `jevmeter` to make a video.\n" if good else "\n  Fix the ❌ items above, then run `jevmeter doctor` again.\n")
    return good


def wizard():
    from .config import get_key
    print("""
   ┌──────────────────────────────────────────────┐
   │  JEVMETER · a live BS meter for any video      │
   └──────────────────────────────────────────────┘
""")
    if not get_key():
        cmd_setup()
    print("🎬  Step 1: your video")
    print("    Drag the video file into this window (or paste a path or link), then press Enter.")
    video = ""
    while not video:
        v = _clean_path(_ask("    Video"))
        if v.startswith(("http://", "https://")) or os.path.exists(v):
            video = v
        else:
            print("    Can't find that file; try dragging it in again.")
    print("\n🎴  Step 2: pick a preset")
    for i, (name, icon, title, desc) in enumerate(PRESET_BLURBS, 1):
        print(f"    {i}. {icon}  {title:32} {desc}")
    choice = _ask("    Number", "1")
    preset = PRESET_BLURBS[int(choice) - 1][0] if choice.isdigit() and 1 <= int(choice) <= len(PRESET_BLURBS) else "debate"
    print("\n👥  Step 3: who is speaking?")
    print("    Two people (debate, interview)? A transcript with `NAME: text` lines gives each one their own meter.")
    tr = _clean_path(_ask("    Transcript file (press Enter to skip = one meter for everyone)", ""))
    args = ["run", video, "--preset", preset]
    if tr:
        if not os.path.exists(tr):
            print("    Transcript not found, continuing without it.")
        else:
            from .transcribe import parse_transcript
            turns = parse_transcript(tr)
            counts = {}
            for t in turns:
                counts[t["speaker"]] = counts.get(t["speaker"], 0) + len(t["text"].split())
            names = sorted(counts, key=counts.get, reverse=True)
            print("    Speakers found: " + ", ".join(f"{n} ({counts[n]} words)" for n in names))
            default = ",".join(names[:2])
            sp = _ask("    Which 1-2 speakers get a meter? (comma separated)", default)
            args += ["--transcript", tr, "--speakers", sp]
    else:
        name = _ask("    Name to show on the meter", "Speaker")
        args += ["--speakers", name]
    print("\n✂️   Step 4: what kind of video do you want?")
    print("    1. Highlights: best moments + hyperlapse + final scoreboard (about 1-2 min, great for posting)")
    print("    2. Full: meter over the whole video (or a part of it)")
    mode = _ask("    Number", "1")
    if mode == "2":
        args += ["--mode", "full"]
        st = _ask("    Start at second (Enter = beginning)", "")
        en = _ask("    End at second (Enter = end)", "")
        if st:
            args += ["--start", st]
        if en:
            args += ["--end", en]
    print("\n🚀  Here we go! Transcribing, scoring every sentence with Jev, then rendering.")
    print("    Tip: next time you can run the same thing directly:")
    print("    jevmeter " + " ".join(f'"{x}"' if " " in x else x for x in args) + "\n")
    main(args)


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return wizard()
    ap = argparse.ArgumentParser(prog="jevmeter", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="transcribe, score with Jev, and render the meter video")
    r.add_argument("video")
    r.add_argument("--transcript", help="speaker-labelled transcript, one `NAME: text` turn per line (recommended for 2 speakers)")
    r.add_argument("--speakers", help="comma list of 1-2 speakers to score (default: the two biggest talkers); without a transcript, the name for the single speaker")
    r.add_argument("--preset", default="debate", help="built-in preset (debate, earnings_call, podcast, sales_pitch) or a JSON path")
    r.add_argument("--mode", choices=["highlights", "full"], default="highlights", help="highlights: auto-picked clips + hyperlapse + end card; full: meter over the whole range")
    r.add_argument("--start", type=float, help="only use the video from this second")
    r.add_argument("--end", type=float, help="only use the video up to this second")
    r.add_argument("--clips", type=int, default=3, help="clips per speaker in highlights mode")
    r.add_argument("--hook", help="opening headline (default from preset; pass '' for none)")
    r.add_argument("--label", action="append", help="display name, e.g. --label TRUMP=Trump (repeatable)")
    r.add_argument("--colors", help="comma list of hex colors per speaker, e.g. '#ff4848,#488cff'")
    r.add_argument("--out", help="output mp4 (default: <video>.jevmeter.mp4)")
    r.add_argument("--work", help="working directory for cached steps (default: <video>.jevmeter/)")
    r.add_argument("--whisper-model", default="small")
    r.add_argument("--language", default="en")
    r.add_argument("--model", default="jev-latest")
    r.add_argument("--threads", type=int, default=6, help="parallel Jev requests")
    r.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) // 2)), help="parallel render processes")
    r.add_argument("--price", type=float, default=0.042, help="$ per 1M input tokens for the cost counter (check console.typesafe.ai)")
    r.add_argument("--score-only", action="store_true", help="stop after scoring and print the summary JSON")
    r.set_defaults(func=cmd_run)
    sub.add_parser("setup", help="save your TypeSafe API key (stored in your user config, never in the project)").set_defaults(func=cmd_setup)
    sub.add_parser("doctor", help="check Python, ffmpeg, speech-to-text and your API key").set_defaults(func=cmd_doctor)
    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
