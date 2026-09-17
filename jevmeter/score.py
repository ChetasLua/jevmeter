"""Score every sentence with TypeSafe's Jev: one request per sentence, one noul question per preset question.

Results are appended to scores.jsonl as they arrive, so an interrupted run resumes where it stopped.
"""
import concurrent.futures as cf
import json
import os
import threading
import time

import requests

from .util import log

API = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
_tl = threading.local()


def _session(key):
    # keep-alive matters: a fresh TLS handshake per call was ~15x slower in testing
    if not hasattr(_tl, "s"):
        _tl.s = requests.Session()
        _tl.s.headers.update({"Authorization": f"Bearer {key}"})
    return _tl.s


def build_states(sents, speakers, preset, history=25):
    """State per scored sentence: who speaks, the latest context turn (e.g. the moderator's question),
    that speaker's earlier sentences, and the answer so far."""
    ctx_label = preset.get("context_label", "context")
    hist = {sp: [] for sp in speakers}
    last_ctx, cur_turn, so_far = "(start)", None, []
    states = []
    for s in sents:
        sp = s["speaker"]
        if sp not in speakers:
            if s["turn"] != cur_turn:
                last_ctx = ""
            last_ctx = (last_ctx + " " + s["text"]).strip()[-700:]
            cur_turn = s["turn"]
            continue
        if s["turn"] != cur_turn:
            so_far, cur_turn = [], s["turn"]
        states.append((s, {
            "speaker": preset.get("speaker_names", {}).get(sp, sp.title()),
            ctx_label: last_ctx,
            "earlier_statements": hist[sp][-history:],
            "current_answer_so_far": " ".join(so_far)[-600:],
            "sentence": s["text"],
        }))
        so_far.append(s["text"])
        hist[sp].append(s["text"])
    return states


def score(sents, speakers, preset, work, key, model="jev-latest", threads=6):
    path = os.path.join(work, "scores.jsonl")
    done = {}
    if os.path.exists(path):
        for line in open(path):
            try:
                r = json.loads(line)
                if "scores" in r:
                    done[r["id"]] = r
            except json.JSONDecodeError:
                pass
    questions = {q["key"]: {"type": "noul", "instructions": q["instructions"]} for q in preset["questions"]}
    todo = [(s, st) for s, st in build_states(sents, speakers, preset) if s["id"] not in done]
    if not todo:
        return done
    log(f"scoring {len(todo)} sentences with {model} ({len(questions)} questions each, {threads} threads)")

    def call(item):
        s, state = item
        err = None
        for attempt in range(6):
            t0 = time.time()
            try:
                r = _session(key).post(API, json={"model": model, "state": state, "questions": questions}, timeout=(6, 30))
                if r.status_code in (401, 403):
                    raise SystemExit(f"TypeSafe API rejected the key ({r.status_code}): {r.text[:200]}")
                r.raise_for_status()
                o = r.json()
                return {"id": s["id"], "ms": round((time.time() - t0) * 1000), "model": o.get("model"),
                        "usage": o.get("usage", {}), "scores": {k: v["noul"] for k, v in o["answers"].items()}}
            except SystemExit:
                raise
            except Exception as e:  # network blips, 429/5xx: back off and retry on a fresh connection
                err = str(e)
                if hasattr(_tl, "s"):
                    del _tl.s
                time.sleep(1.0 * (attempt + 1))
        return {"id": s["id"], "error": err}

    t0, n_err = time.time(), 0
    with open(path, "a") as f, cf.ThreadPoolExecutor(threads) as ex:
        futs = [ex.submit(call, it) for it in todo]
        for i, fut in enumerate(cf.as_completed(futs)):
            r = fut.result()
            f.write(json.dumps(r) + "\n")
            f.flush()
            if "scores" in r:
                done[r["id"]] = r
            else:
                n_err += 1
            if i % 100 == 0 or i == len(todo) - 1:
                log(f"  {i + 1}/{len(todo)}  {time.time() - t0:.0f}s")
    if n_err:
        log(f"{n_err} sentences failed after retries; re-run the same command to retry just those")
    return done
