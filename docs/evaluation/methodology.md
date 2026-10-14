# Evaluation methodology

This repository publishes no benchmark results and no model rankings.
Results depend on your prompts, your vendor deployments and model versions,
and your contracted prices; they come from running the harness against your
own deployments. The shipped golden sets (`configs/golden-sets/`) are
reviewed starting sets for three families, not a benchmark.

## Golden sets

A golden set is a YAML file with a name, version, task family, description,
data classification, routing constraints, optional system prompt, a default
grader and cases (`id`, `input`, `expected`, optional per-case grader). Its
canonical SHA-256 digest is recorded with every run and copied into
promotion evidence, so the exact set behind a decision is provable. Changing
a case requires bumping `version`.

Runs are compliance filtered like routed traffic: a provider that may not
receive the set's classification or constraints is excluded and listed in
`summary.excluded`; only cleared judges grade.

## Graders

| Grader | Score | Pass |
|---|---|---|
| `exact` | 1 or 0 after optional whitespace collapsing and case folding | score = 1 |
| `regex` | Share of patterns that match (`search` or `fullmatch`) | all match |
| `json_schema` | 0 if the output has no JSON or violates the Draft 2020-12 schema; otherwise the share of `expected` fields that match (strings compared case- and whitespace-insensitively) | score = 1 |
| `llm_judge` | Judge score s in 1..5 (calibrated when available) mapped to (s - 1) / 4 | s >= `pass_threshold` |

JSON is extracted from bare JSON, a fenced block, or the outermost braces.

## LLM judge

The judge receives the rubric, the prompt, the response and an optional
reference answer. Prompt, response and reference are wrapped in
`untrusted_content` blocks with random boundaries. Before any call, the
response is scanned with agentic-core's injection detector; a response that
tries to instruct the judge (score at least 0.5) fails with score 1 and no
judge call. The judge is chosen from the grader's `judge_providers`, minus
the provider being graded, minus any judge the compliance filter does not
clear for the data.

## Judge calibration

Given n items with human scores h_i and judge scores j_i on the 1..5 scale:

- **Quadratic-weighted kappa:** with observed matrix O, expected matrix
  E_ab = (row_a * col_b) / n and weights w_ab = (a - b)^2 / (k - 1)^2,
  kappa = 1 - sum(w * O) / sum(w * E). It rewards near-agreement and is
  chance-corrected.
- **Spearman correlation** on average ranks (ties share a rank).
- **Exact agreement** and **mean bias** = mean(j - h).
- **Isotonic map:** pool-adjacent-violators regression of h on j gives a
  non-decreasing step function through the distinct judge scores; values in
  between are linearly interpolated and values outside are clamped.

A calibration is accepted (`calibrated: true`) when n >= `MRE_MIN_CALIBRATION_ITEMS`
and kappa >= `MRE_MIN_JUDGE_KAPPA`. It applies to one (judge provider,
rubric digest) pair; a rubric edit needs a new calibration.

## Summary statistics per provider

- Pass rate with a 95% **Wilson score interval**:
  centre = (p + z^2/2n) / (1 + z^2/n),
  margin = z * sqrt(p(1-p)/n + z^2/4n^2) / (1 + z^2/n), z = 1.96.
  Errors (unavailable provider, blocked output, judge failure) count as fails.
- Mean score, p50 and p95 latency (linear interpolation) over successful
  cases, total cost and cost per pass.

## Pairwise preference and Bradley-Terry ranking

With `pairwise: true`, every pair of providers is compared on every case
where both produced an output. The judge sees the two responses as A and B,
then again as B and A. If the two verdicts agree (after mapping back), that is
the result; otherwise the comparison is a tie. A response that tries to
instruct the judge loses the comparison (both manipulating: tie).

Bradley-Terry: P(i beats j) = p_i / (p_i + p_j). With W_ij the wins of i over
j (a tie adds 0.5 to each side) plus 0.05 virtual wins each way per pair
(0.1 virtual ties), the MM update is

p_i <- W_i / sum_{j != i} (N_ij / (p_i + p_j)),

followed by normalisation to sum 1, until the largest change is below 1e-10.
The virtual ties keep a winless provider's strength positive and guarantee
convergence. 95% intervals are the 2.5th and 97.5th percentiles over 200
bootstrap refits that resample golden cases with replacement, keeping all
comparisons of a case together.

## Offline prior

After a completed run, every successful case result is added to the family
arm (bucket `*`) of the routing bandit as an observation of quality, latency
and cost with weight `offline_prior_weight` (default 0.25). Online evidence
therefore outweighs offline evidence case for case, while new providers do
not start from an uninformed prior.

## Online shadow evaluation

For a family with `shadow.rate` r, each routed request is mirrored with
probability r to every shadow provider that passes the same compliance filter
as the primary request (excluding the primary provider). Before each shadow
call the service checks the day's `shadow` ledger total against
`MRE_SHADOW_DAILY_BUDGET_USD`; when exhausted, shadows stop until the next UTC
day. Grading:

- with a `response_schema`: both answers are schema-graded; the shadow's
  score is its quality observation;
- otherwise, with `shadow.judge_providers`: pairwise comparison by a judge
  that is neither provider and is cleared for the data; the shadow arm
  receives 1 / 0.5 / 0 for win / tie / loss and the primary arm the
  complement, both at weight 0.5;
- with neither: only latency and cost are learned.

Shadow answers never reach callers; their cost is ledgered as `shadow`.

## Promotion gates

Per family (`promotion` in `configs/routing.yaml`): minimum number of cases,
minimum pass rate, and optionally a calibrated judge. Evidence is checked when
requested and again when approved.

## Reproducing results

1. Calibrate each judge with at least `MRE_MIN_CALIBRATION_ITEMS`
   human-scored items per rubric.
2. Run each golden set against the providers you can use, with
   `pairwise: true` where ranking matters.
3. Read `GET /v1/evaluations/{run_id}` and `GET /v1/reports/{family}`.
4. Keep run IDs: they are the evidence for promotions.
