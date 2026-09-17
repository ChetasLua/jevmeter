"""Turn sentences + Jev scores into a timeline, flag thresholds, a summary and an edit decision list."""
import json
import os
import statistics as st

from .util import log


def pick_speakers(sents, requested=None):
    names = {}
    for s in sents:
        names[s["speaker"]] = names.get(s["speaker"], 0) + len(s["text"].split())
    if requested:
        out = []
        for r in requested:
            r = r.strip().upper()
            hit = next((n for n in names if n == r), None) or next((n for n in names if r in n), None)
            if not hit:
                raise SystemExit(f"speaker {r!r} not in transcript; found: {', '.join(sorted(names))}")
            out.append(hit)
        return out[:2]
    top = sorted(names, key=names.get, reverse=True)
    # moderators/hosts usually speak less than the people being scored; take the two biggest talkers
    return top[:2] if len(top) >= 2 else top


def build_timeline(sents, scores, speakers, preset, work):
    rows = []
    for s in sents:
        if s["speaker"] in speakers and s["id"] in scores:
            r = scores[s["id"]]
            rows.append({**s, "scores": r["scores"], "ms": r["ms"], "tokens": r.get("usage", {}).get("input_tokens", 0),
                         "output_tokens": r.get("usage", {}).get("output_tokens", 0), "model": r.get("model")})
    if not rows:
        raise SystemExit("nothing scored — check the speaker names and the API key")
    rows.sort(key=lambda r: r["end"])
    qs = preset["questions"]
    thresholds = {}
    for q in qs:
        vals = sorted(r["scores"][q["key"]] for r in rows)
        auto = vals[min(len(vals) - 1, int(len(vals) * 0.98))]
        thresholds[q["key"]] = q.get("flag") if isinstance(q.get("flag"), (int, float)) else max(0.5, round(auto, 2))
    idx_keys = [q["key"] for q in qs if q.get("index", False)]
    summary = {"speakers": {}, "sentences": len(rows), "input_tokens": sum(r["tokens"] for r in rows),
               "output_tokens": sum(r["output_tokens"] for r in rows), "median_ms": st.median(r["ms"] for r in rows),
               "models": sorted({r["model"] for r in rows if r["model"]}), "thresholds": thresholds, "index_keys": idx_keys}
    for sp in speakers:
        rs = [r for r in rows if r["speaker"] == sp]
        if not rs:
            continue
        summary["speakers"][sp] = {"sentences": len(rs), **{q["key"]: round(st.mean(r["scores"][q["key"]] for r in rs), 4) for q in qs},
                                   "index": round(st.mean(sum(r["scores"][k] for k in idx_keys) / max(1, len(idx_keys)) for r in rs), 4)}
    tl = {"sentences": rows, "speakers": speakers, "summary": summary}
    json.dump(tl, open(os.path.join(work, "timeline.json"), "w"))
    json.dump(summary, open(os.path.join(work, "summary.json"), "w"), indent=2)
    return tl


def plan_highlights(tl, preset, clips_per_speaker=3, min_len=10, max_len=15.5, gap=None, hyper=16, end=17, first_after=0):
    rows = sorted(tl["sentences"], key=lambda r: r["start"])
    if gap is None:  # keep clips spread out, scaled to the video's length
        gap = min(90.0, (rows[-1]["end"] - rows[0]["start"]) / (4 * max(1, clips_per_speaker)))
    thr = tl["summary"]["thresholds"]
    idx = tl["summary"]["index_keys"]
    val = lambda r: sum(r["scores"][k] for k in idx) / max(1, len(idx))
    flagged = lambda r: any(r["scores"][k] >= thr[k] for k in thr)
    cands = []
    for i, r in enumerate(rows):
        if r.get("interpolated") or r["start"] < first_after:
            continue
        j = i
        while j + 1 < len(rows) and rows[j + 1]["turn"] == r["turn"] and rows[j + 1]["speaker"] == r["speaker"] \
                and not rows[j + 1].get("interpolated") and rows[j + 1]["end"] - r["start"] <= max_len:
            j += 1
        seg = rows[i:j + 1]
        d = seg[-1]["end"] - r["start"]
        if d < min_len:
            continue
        score = st.mean(val(x) for x in seg) + 0.03 * sum(flagged(x) for x in seg)
        cands.append((score, r["speaker"], r["start"], seg[-1]["end"]))
    cands.sort(reverse=True)
    picks = {sp: [] for sp in tl["speakers"]}
    for c in cands:
        p = picks[c[1]]
        if len(p) < clips_per_speaker and all(abs(c[2] - q[2]) > gap for q in p):
            p.append(c)
    # interleave speakers, strongest first, so the video alternates and stays symmetric
    order = []
    for k in range(clips_per_speaker):
        for sp in tl["speakers"]:
            if k < len(picks[sp]):
                order.append(picks[sp][k])
    if not order:
        raise SystemExit("no 10-15 s single-speaker stretches found; try --mode full")
    edl = [{"type": "clip", "src": round(c[2] - 0.35, 2), "dur": round(c[3] - c[2] + 0.85, 2)} for c in order]
    edl[0]["hook"] = True
    lo_t, hi_t = rows[0]["start"], rows[-1]["end"]
    edl.append({"type": "hyper", "from": lo_t, "to": hi_t, "dur": hyper})
    edl.append({"type": "end", "dur": end, "still": (lo_t + hi_t) * 0.8})
    log(f"highlights: {len(order)} clips, {sum(s['dur'] for s in edl):.0f}s total")
    return edl


def plan_full(tl, start=None, end=None, video_dur=None, end_card=12):
    rows = tl["sentences"]
    a = start if start is not None else max(0.0, rows[0]["start"] - 1)
    b = end if end is not None else min(video_dur or 1e12, max(r["end"] for r in rows) + 1)
    return [{"type": "clip", "src": round(a, 2), "dur": round(b - a, 2), "hook": True},
            {"type": "end", "dur": end_card, "still": (a + b) / 2}]
