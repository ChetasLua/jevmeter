# Preset evaluation

How well each preset question separates sentences that should score **yes** from sentences that should score **no**.

- **Held-out test set** (`test_set.json`): 200 sentences, 5 yes and 5 no per question. Written independently of the
  presets, and at least 3 of every 5 "no" items are hard near-misses. Never used for tuning.
- **Tuning set** (`dev_set.json`): 135 sentences used to write the criteria.
- Model `jev-latest` (jev-1.13.0), one request per item, measured 2026-09-17. `jev-preview` scored the same within 0.001 AUC.

| | accuracy at 0.5 | mean score on "no" items | AUC |
|---|---|---|---|
| original presets | 94.5% | 0.16 | 0.998 |
| **shipped presets** | **99.0%** | **0.08** | **1.000** |

What changed, following the TypeSafe docs:
1. Every question names the field it judges, like `` `sentence` `` or `` `moderator_question` ``.
2. Every question has structured `criteria` for yes and no, each with a short definition and examples.
3. The "no" side spells out the near-misses: a courtesy before a real answer, a promise that contains a number,
   hedged quantifiers, rebutting someone else's claim.
4. "Factual claim" means past or present facts only, so promises and targets no longer count.

## Per question (held-out test set)

| preset/question | acc before | acc after | "no" mean before | "no" mean after | "yes" mean before | "yes" mean after |
|---|---|---|---|---|---|---|
| `debate/contradicts_earlier` | 100% | **100%** | 0.10 | **0.05** | 0.96 | 0.96 |
| `debate/dodged_question` | 100% | **100%** | 0.15 | **0.05** | 0.94 | 0.95 |
| `debate/emotional_appeal` | 100% | **100%** | 0.16 | **0.08** | 0.96 | 0.97 |
| `debate/evasive` | 100% | **100%** | 0.07 | **0.04** | 0.92 | 0.96 |
| `debate/factual_claim` | 80% | **100%** | 0.29 | **0.11** | 0.98 | 0.97 |
| `earnings_call/blame_external` | 100% | **100%** | 0.11 | **0.03** | 0.94 | 0.97 |
| `earnings_call/dodged_question` | 100% | **100%** | 0.07 | **0.03** | 0.96 | 0.96 |
| `earnings_call/hype_language` | 100% | **100%** | 0.05 | **0.05** | 0.96 | 0.97 |
| `earnings_call/specific_number` | 90% | **100%** | 0.02 | **0.05** | 0.88 | 0.99 |
| `earnings_call/vague_guidance` | 100% | **100%** | 0.04 | **0.02** | 0.93 | 0.96 |
| `podcast/emotional_appeal` | 100% | **100%** | 0.12 | **0.05** | 0.95 | 0.93 |
| `podcast/factual_claim` | 100% | **100%** | 0.17 | **0.06** | 0.95 | 0.96 |
| `podcast/overgeneralization` | 70% | **90%** | 0.59 | **0.19** | 0.98 | 0.98 |
| `podcast/self_promotion` | 100% | **100%** | 0.05 | **0.02** | 0.97 | 0.98 |
| `podcast/unsupported` | 80% | **100%** | 0.37 | **0.13** | 0.95 | 0.97 |
| `sales_pitch/buzzwords` | 100% | **100%** | 0.09 | **0.07** | 0.94 | 0.95 |
| `sales_pitch/concrete_metric` | 90% | **100%** | 0.16 | **0.07** | 0.99 | 0.99 |
| `sales_pitch/overpromise` | 90% | **90%** | 0.31 | **0.18** | 0.97 | 0.98 |
| `sales_pitch/urgency_pressure` | 90% | **100%** | 0.18 | **0.14** | 0.97 | 0.96 |
| `sales_pitch/vague_benefit` | 100% | **100%** | 0.16 | **0.09** | 0.87 | 0.95 |

## Reproduce

```bash
export TYPESAFE_API_KEY=...
python eval/run_eval.py --set eval/test_set.json                          # shipped presets
python eval/run_eval.py --set eval/test_set.json --presets my_presets/    # your own presets
```

A small labelled set can't prove the presets are right on every video. These numbers show the questions are
unambiguous on clear-cut and near-miss sentences; real speech is messier.
