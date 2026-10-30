# Aletheia research compass

Status: methodology `TF-2D/1.0`, approved by the principal researcher on
2026-10-18. This version supersedes the 2026-09-28 score architecture while
preserving its provenance and all historical outputs.

## General research question

How, and under what conditions, do holders of public power relate fidelity to
the truth knowable at the time to the construction of narrative order during
contested and evolving events?

Operational version:

> How do news organizations select, formulate, attribute, organize and update
> factual claims, and what configurations of truth fidelity and frame intensity
> emerge across cases, languages, phases and publishers?

The framework is deliberately broader than journalism. The present empirical
chapter examines news organizations because that is the corpus currently
collected. The research measures observable communication and correction
trajectories. It does not infer whether a journalist privately intended to
deceive; claims about motive would require different evidence, such as
interviews or internal records.

## Core concepts

### T — truth fidelity

`T` measures:

- alignment with the best evidence available at publication time;
- transparency and traceability of sources and evidence;
- confidence proportionate to what was then knowable;
- correction or updating when the evidence changes.

`T` is reported on a 0–100 scale when coverage requirements are met. Higher
values indicate stronger fidelity to the defined evidentiary criteria, not an
all-purpose measure of journalistic quality.

### F — frame intensity

`F` measures how saliently, consistently and coherently an item organizes its
material into an interpretive frame. It does not measure truth, quality,
fairness, balance, bias, political desirability or malice. Strong framing is an
ordinary feature of communication; a high `F` is neither good nor bad by
itself.

### N — diagnostic edge indicator

`N` is not a core axis. A component from the historical N battery may be used
only to flag a possible severe departure when all of the following are present:

1. a concrete claim, omission, headline-body gap or newsroom-owned formulation
   is identifiable in the frozen text;
2. a specific contemporaneous anchor or a specific internal textual
   contradiction establishes the evidentiary comparison;
3. the departure is material to the account rather than a stylistic
   disagreement;
4. the evidence span and anchor are preserved for human review.

An N flag is a review route, not a verdict. It cannot prove intent, malice or
conscious deception and may not contribute to a summary score, source ranking
or central comparison among cases.

## Outcome architecture: the T × F map

There is no combined Aletheia score. `T` and `F` are reported separately and
the central result is a two-dimensional position. The four analytic regions
are descriptive configurations, not automatic moral judgments:

| Region | Descriptive reading | What it does not establish |
| --- | --- | --- |
| High T / High F | Strong evidentiary fidelity and a strongly organized interpretive frame | that the frame is neutral, desirable or beyond criticism |
| High T / Low F | Strong evidentiary fidelity with relatively weak or diffuse framing | that the item is superior, complete or more useful |
| Low T / High F | A strongly organized frame with weak fidelity to the defined evidence criteria | deception, malice or conscious manipulation |
| Low T / Low F | Weak fidelity and weak or diffuse organization | motive; the pattern may also reflect noise, incompleteness or genre constraints |

“High” and “low” must be defined by preregistered thresholds or transparent
sample-relative summaries. They may not be chosen after inspecting a desired
case result.

## Aggregation

- Claim mentions are the unit for factual assertion, attribution and
  certainty.
- Articles are the unit for source transparency, framing behavior, headlines
  and language.
- A three-phase publisher slot is the unit for correction and updating.
- Each stratum is the equal-weight mean of its approved publisher slots.
- The case-level summary is the equal-weight mean of the three strata.
- Approved substitute pairs remain one research slot.
- `T` and `F` retain separate coverage indicators and uncertainty summaries.

No `T/F` weighting, scalar ranking or combined interpretation label is
permitted. Historical `A` and aggregate `N` values are retained unchanged as
`legacy/audit data` solely for reproducibility.

## Transition and provenance

The earlier architecture used `A = 0.65 × T + 0.35 × (100 − N)` and treated F
as an additional descriptive axis. The principal researcher retired that
architecture on 2026-10-18 because it collapsed the theoretical tension into a
single compensatory number and because the N calibration displayed a strong
floor effect. The frozen N calibration sample is documented as an incomplete
methodological experiment and does not block new cases.

The four closed cases are not rerun. Full F coverage was independently checked
for 108/108 articles in `analysis/frame_intensity_coverage_audit_v1.json`:

- CASE-001: mean F 75.3704;
- CASE-002: mean F 79.8148;
- CASE-003: mean F 68.8889;
- CASE-004: mean F 76.4815.

## Methodological basis

- [Entman (1993), framing](https://doi.org/10.1111/j.1460-2466.1993.tb01304.x)
  supports treating selection, salience, causal interpretation and moral
  evaluation as observable framing operations.
- [Bennett (1990), press-state relations](https://doi.org/10.1111/j.1460-2466.1990.tb02265.x)
  and [Entman (2003), cascading activation](https://doi.org/10.1080/10584600390244176)
  support examining how frames held by powerful actors enter news discourse.
- [Krippendorff (2004)](https://doi.org/10.1007/s11135-004-8107-7) and
  [Matthes (2008)](https://doi.org/10.1111/j.1460-2466.2008.00384.x) inform
  reliability and validity checks for content coding.

The OECD/JRC composite-indicator handbook remains in the historical source
record because it informed the retired architecture. It is not the governing
basis for the current non-composite outcome.
