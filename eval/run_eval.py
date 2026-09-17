"""Measure how well each preset question separates yes from no on a labelled set.

    TYPESAFE_API_KEY=... python eval/run_eval.py --set eval/dev_set.json --presets jevmeter/presets --model jev-latest
Prints per-question AUC, accuracy at 0.5 and mean score for positives/negatives. Results are cached in eval/cache/.
"""
import argparse, concurrent.futures as cf, hashlib, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from jevmeter.score import make_questions, make_state, _session, API

HERE = os.path.dirname(os.path.abspath(__file__))


def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=os.path.join(HERE, "dev_set.json"))
    ap.add_argument("--presets", default=os.path.join(HERE, "..", "jevmeter", "presets"))
    ap.add_argument("--model", default="jev-latest")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--json", help="write the metrics here")
    a = ap.parse_args()
    key = os.environ["TYPESAFE_API_KEY"]
    items = json.load(open(a.set))
    presets = {}
    for f in os.listdir(a.presets):
        if f.endswith(".json"):
            p = json.load(open(os.path.join(a.presets, f)))
            presets[p["name"]] = p
    cache_dir = os.path.join(HERE, "cache")
    os.makedirs(cache_dir, exist_ok=True)

    def job(it):
        p = presets[it["preset"]]
        q = make_questions(p)[it["question"]]
        state = make_state(p, "Speaker", it["context"], it["earlier_statements"], it["answer_so_far"], it["sentence"])
        body = {"model": a.model, "state": state, "questions": {"q": q}}
        h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
        path = os.path.join(cache_dir, h + ".json")
        if os.path.exists(path):
            return it, json.load(open(path))["noul"]
        for attempt in range(6):
            try:
                r = _session(key).post(API, json=body, timeout=(6, 30))
                r.raise_for_status()
                v = r.json()["answers"]["q"]["noul"]
                json.dump({"noul": v}, open(path, "w"))
                return it, v
            except Exception as e:
                time.sleep(1 + attempt)
        return it, None

    t0 = time.time()
    with cf.ThreadPoolExecutor(a.threads) as ex:
        res = list(ex.map(job, items))
    rows, metrics = {}, {}
    for it, v in res:
        if v is None:
            continue
        rows.setdefault((it["preset"], it["question"]), []).append((it["label"], v))
    print(f"{'preset':14} {'question':22} {'AUC':>5} {'acc':>5} {'pos':>5} {'neg':>5}")
    for (pr, q), rs in sorted(rows.items()):
        pos = [v for l, v in rs if l == 1]
        neg = [v for l, v in rs if l == 0]
        acc = sum((v >= 0.5) == (l == 1) for l, v in rs) / len(rs)
        m = {"auc": auc(pos, neg), "acc": acc, "pos": sum(pos) / len(pos), "neg": sum(neg) / len(neg)}
        metrics[f"{pr}/{q}"] = m
        print(f"{pr:14} {q:22} {m['auc']:5.2f} {m['acc']:5.2f} {m['pos']:5.2f} {m['neg']:5.2f}")
    for pr in sorted({k.split('/')[0] for k in metrics}):
        ms = [v for k, v in metrics.items() if k.startswith(pr + "/")]
        print(f"== {pr:11} mean AUC {sum(m['auc'] for m in ms)/len(ms):.3f}  mean acc {sum(m['acc'] for m in ms)/len(ms):.3f}")
    allm = list(metrics.values())
    print(f"== ALL         mean AUC {sum(m['auc'] for m in allm)/len(allm):.3f}  mean acc {sum(m['acc'] for m in allm)/len(allm):.3f}   ({time.time()-t0:.0f}s, {a.model})")
    if a.json:
        json.dump(metrics, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
