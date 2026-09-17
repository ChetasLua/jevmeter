"""Word-level transcription (mlx-whisper on Apple Silicon, faster-whisper elsewhere) and sentence timing.

Two ways to get sentences:
  * with a speaker-labelled transcript (NAME: text lines): the transcript text is authoritative and each
    sentence gets its timestamps from whisper words via sequence alignment;
  * without one: whisper's own words are split into sentences and attributed to a single speaker.
"""
import difflib
import json
import os
import re

from .util import load_audio, log

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())


def whisper_words(video, model="small", language="en", start=None, dur=None):
    audio = load_audio(video, start=start, dur=dur)
    off = start or 0.0
    try:
        import mlx_whisper
        repo = model if "/" in model else f"mlx-community/whisper-{model}-mlx"
        log(f"transcribing with mlx-whisper ({repo}), {len(audio)/16000/60:.1f} min of audio")
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=repo, word_timestamps=True, language=language,
                                   condition_on_previous_text=False, verbose=None)
        return [{"w": w["word"], "s": w["start"] + off, "e": w["end"] + off}
                for seg in r["segments"] for w in seg.get("words", [])]
    except ImportError:
        pass
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("No whisper backend. Install one:  pip install 'jevmeter[mlx]'  (Apple Silicon)  or  pip install 'jevmeter[cpu]'")
    log(f"transcribing with faster-whisper ({model}), {len(audio)/16000/60:.1f} min of audio")
    wm = WhisperModel(model, compute_type="int8")
    segs, _ = wm.transcribe(audio, word_timestamps=True, language=language, condition_on_previous_text=False)
    return [{"w": w.word, "s": w.start + off, "e": w.end + off} for seg in segs for w in (seg.words or [])]


def parse_transcript(path):
    """`NAME: text` starts a turn; other non-empty lines continue the previous turn."""
    turns = []
    head = re.compile(r"^\s*([A-Za-z][A-Za-z .'\-]{0,60}?)\s*:\s+(.*)$")
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        m = head.match(line)
        if m and len(m.group(1).split()) <= 5 and m.group(1)[0].isupper():
            turns.append({"speaker": m.group(1).strip().upper(), "text": m.group(2).strip()})
        elif turns:
            turns[-1]["text"] += " " + line
    if not turns:
        raise SystemExit(f"{path}: no `NAME: text` lines found")
    # some published transcripts (e.g. a news page) contain the whole transcript twice; keep one copy
    n = len(turns)
    if n % 2 == 0 and n > 2 and turns[: n // 2] == turns[n // 2:]:
        log(f"transcript repeats itself after {n // 2} turns; keeping the first copy")
        turns = turns[: n // 2]
    return turns


def split_sentences(text):
    return [s.strip() for s in SENT_SPLIT.split(text) if s.strip()]


def align(turns, words):
    """Give every transcript sentence start/end times and per-word times from whisper words."""
    ref, meta = [], []
    sents = []
    for ti, t in enumerate(turns):
        for si, s in enumerate(split_sentences(t["text"])):
            sents.append({"turn": ti, "sent": si, "speaker": t["speaker"], "text": s})
            for tok in s.split():
                if norm(tok):
                    ref.append(norm(tok))
                    meta.append(len(sents) - 1)
    hyp = [norm(w["w"]) for w in words]
    sm = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    times = [None] * len(ref)
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            times[a + k] = (words[b + k]["s"], words[b + k]["e"])
    matched = sum(t is not None for t in times) / max(1, len(ref))
    log(f"aligned {matched:.1%} of transcript words to audio")
    per = {}
    for i, si in enumerate(meta):
        per.setdefault(si, []).append(times[i])
    for si, s in enumerate(sents):
        ts = per.get(si, [])
        # word list follows s["text"].split(); tokens that normalise to "" get None
        toks = s["text"].split()
        it = iter(ts)
        s["words"] = [next(it, None) if norm(tok) else None for tok in toks]
        got = [t for t in ts if t]
        s["coverage"] = len(got) / max(1, len(ts))
        s["start"] = min(t[0] for t in got) if got else None
        s["end"] = max(t[1] for t in got) if got else None
    _fill_gaps(sents)
    return sents, matched


def from_words(words, speaker="SPEAKER"):
    sents, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.!?][\"')\]]?$", w["w"].strip()) and len(cur) >= 2:
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    out = []
    for i, ws in enumerate(sents):
        text = " ".join(w["w"].strip() for w in ws).strip()
        out.append({"turn": i, "sent": 0, "speaker": speaker, "text": text, "coverage": 1.0,
                    "start": ws[0]["s"], "end": ws[-1]["e"], "words": [(w["s"], w["e"]) for w in ws]})
    return out


def _fill_gaps(sents):
    good = lambda s: s["start"] is not None and s["coverage"] >= 0.3 and s["end"] - s["start"] < 60
    for i, s in enumerate(sents):
        if good(s):
            continue
        prev = next((sents[j]["end"] for j in range(i - 1, -1, -1) if good(sents[j])), 0.0)
        nxt = next((sents[j]["start"] for j in range(i + 1, len(sents)) if good(sents[j])), prev + 3)
        s["start"], s["end"], s["interpolated"] = prev, max(prev + 0.5, nxt), True
    for i in range(1, len(sents)):
        if sents[i]["start"] < sents[i - 1]["start"]:
            sents[i]["start"] = sents[i - 1]["start"]
        sents[i]["end"] = max(sents[i]["end"], sents[i]["start"] + 0.3)


def build_sentences(video, work, transcript=None, model="small", language="en", start=None, end=None, speaker_name=None):
    wpath = os.path.join(work, "whisper_words.json")
    if os.path.exists(wpath):
        words = json.load(open(wpath))
    else:
        dur = (end - (start or 0)) if end is not None else None
        words = whisper_words(video, model, language, start, dur)
        json.dump(words, open(wpath, "w"))
    if transcript:
        turns = parse_transcript(transcript)
        if start is not None or end is not None:
            log("note: --start/--end with a transcript still aligns the whole transcript; out-of-range sentences are dropped")
        sents, _ = align(turns, words)
        lo, hi = (start or 0), (end or 1e12)
        sents = [s for s in sents if not s.get("interpolated") or lo <= s["start"] <= hi]
        sents = [s for s in sents if lo - 1 <= s["start"] <= hi + 1]
    else:
        sents = from_words(words, (speaker_name or "SPEAKER").upper())
    for i, s in enumerate(sents):
        s["id"] = i
    json.dump(sents, open(os.path.join(work, "sentences.json"), "w"))
    return sents
