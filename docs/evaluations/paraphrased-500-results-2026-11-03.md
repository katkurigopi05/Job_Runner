# 500 paraphrased questions: results, 2026-11-03

`make eval-paraphrased mode=shipped` and `mode=qwen_semantic` over
[`paraphrased_questions_500.md`](paraphrased_questions_500.md), one read-only snapshot of the
live database each (19,245 open postings, measured 2026-11-04 04:44 and 05:22 UTC). Every target
was open and inside the search area; none was skipped. Per-question outcomes:
[`paraphrased-500-2026-11-03.json`](paraphrased-500-2026-11-03.json).

One target a question: a list of five other postings that answer the question as well scores as a
miss. This measures whether the posting a question was written from is found, not relevance.

## Overall

| Search | First | In the five shown | In 20 | Listed | MRR | In 5 with twins | Median s |
|---|---:|---:|---:|---:|---:|---:|---:|
| Shipped | 203 (41%) | 330 (66%) | 385 (77%) | 389 (78%) | 0.517 | 334 | 4.21 |
| Qwen + semantic candidates | 179 (36%) | 358 (72%) | 447 (89%) | 461 (92%) | 0.510 | 360 | 5.78 |

Question by question, Qwen + semantic against shipped:

- **In the five shown: +5.6 points** (95% bootstrap interval +1.8 to +9.4). 62 targets came into the five, 34 left it.
- **MRR: -0.007** (-0.036 to +0.022), no change. Semantic candidates reach more targets and rank more of them second: 60 targets first under shipped were not first with Qwen, nearly all at 2 or 3.
- The target ranked higher in 172 questions, lower in 117, the same in 211.

**Times are not a measurement of either mode.** The Mac was running other work throughout
(a Kaggle grading run's client, a test suite during the first attempt). The same shipped search
took a median 2.21 s in the first attempt and 4.21 s here. The graded comparison on 2026-11-03
(`.claude/rules/assistant-search.md`) is the latency evidence: +0.58 s.

## By family

| Family | Questions | In 5, shipped | In 5, Qwen + semantic | MRR, shipped | MRR, Qwen + semantic |
|---|---:|---:|---:|---:|---:|
| Insurance and claims | 15 | 10 | 11 | 0.541 | 0.480 |
| Hardware, manufacturing and trades | 31 | 21 | 24 | 0.504 | 0.529 |
| Software engineering | 40 | 26 | 25 | 0.501 | 0.461 |
| Machine learning and AI | 30 | 18 | 21 | 0.494 | 0.530 |
| Security | 30 | 18 | 22 | 0.437 | 0.562 |
| Data and analytics | 30 | 20 | 21 | 0.468 | 0.470 |
| Solutions, sales and field engineering | 30 | 16 | 14 | 0.471 | 0.366 |
| Infrastructure, SRE and IT | 30 | 25 | 28 | 0.630 | 0.719 |
| Sales and partnerships | 41 | 25 | 26 | 0.529 | 0.448 |
| Operations, programs and supply chain | 26 | 23 | 23 | 0.730 | 0.632 |
| Marketing and communications | 27 | 23 | 25 | 0.632 | 0.662 |
| Science and R&D | 15 | 10 | 13 | 0.605 | 0.615 |
| Other | 40 | 24 | 27 | 0.439 | 0.378 |
| Product management | 17 | 9 | 9 | 0.384 | 0.340 |
| Finance and accounting | 17 | 13 | 14 | 0.607 | 0.590 |
| Healthcare and clinical | 21 | 14 | 15 | 0.584 | 0.493 |
| Legal, policy and compliance | 15 | 10 | 10 | 0.544 | 0.512 |
| Customer success and support | 15 | 7 | 11 | 0.308 | 0.460 |
| Design | 15 | 12 | 11 | 0.658 | 0.596 |
| People, HR and recruiting | 15 | 6 | 8 | 0.250 | 0.463 |

Weakest under shipped: people/HR/recruiting (6 of 15 in five), customer success and support (7 of 15),
product management (9 of 17). Qwen + semantic lifts customer success to 11 and security from 18 to
22, and lowers solutions/field engineering from 16 to 14. Fifteen to forty questions a family:
read these as directions, not measurements.

## The 15 trap wordings

Asked in their first wording (Appendix A of the question file), all 15 found their target 0 times
in either mode, in a median 0.2-0.5 s: the company reader takes a common word ("front desk",
"ramp", "at scale", "pay-later") for a hiring employer and the search answers about that
employer only. Reworded, the same postings are found as often as the rest. Not fixed here.

## A crash this set found

The first Qwen + semantic attempt raised `the number of query arguments cannot exceed 32767`
partway through: a question whose filters kept few candidates widened the 1-bit shortlist past
32,767 chunks, and the ids went back to Postgres one parameter each. Fixed on branch
`fix/qwen-shortlist-params` (`= ANY(:ids)`, one array parameter), and this run was made with
that fix applied: 0 errors in 1,030 questions. The scorer now records a question whose search
raises as an error in its own column, never as a miss.
