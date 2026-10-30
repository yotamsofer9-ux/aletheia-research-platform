# Aletheia frame-intensity contract — version 0.2.0

Status: methodology-aligned on 2026-10-18 under `TF-2D/1.0`. The 0.1.0
measurement outputs remain valid and are not rerun.

## Construct boundary

`F` describes how strongly a frozen news item organizes available material into
a salient and coherent interpretive frame. It does not judge factual accuracy,
fairness, political desirability, viewpoint balance, or compliance with the
evidence. High frame intensity is not a defect.

`T` independently measures truth fidelity. `F` and `T` are the two core axes
and are shown as a two-dimensional `T × F` position; they are never combined.
`N` is an edge-case diagnostic only. There is no active `A` result. This is a
permanent construct boundary: frame strength may not compensate arithmetically
for factual or evidentiary weakness. Existing `T/N/A` fields remain immutable
as `legacy/audit data`.

The research compares observable truth fidelity with the construction of
narrative order. It does not infer the journalist's private intent. Neither a
high F nor any T×F region establishes bias, malice, deception or quality.

## Unit and scope

The unit is one frozen article or inspectable news item in its original
language. The coder must inspect the full available text, including headline,
lead, body and conclusion. Inspectable captions or transcripts may be included.
Unavailable visual content must be declared; it may never be inferred.

Every analysis records:

- a concise, neutral Hebrew frame statement;
- one to three descriptive frame families;
- exact original-language frame cues;
- each cue's textual location, ownership, function and rhetorical device;
- `F1`–`F5`, each on a 0–4 scale;
- confidence, rationale and cue references for every component;
- review and coverage limitations.

## Ownership

Frame-cue ownership is one of:

- `OUTLET`: unambiguously newsroom-owned narration or formulation;
- `ATTRIBUTED_ACTOR`: attributed speech or position;
- `HEADLINE_SELECTED_QUOTE`: attributed language deliberately foregrounded in
  the headline or subheadline;
- `UNCLEAR`: ownership cannot be resolved from the frozen item.

Attribution does not become outlet endorsement. Prominent selection and
repetition may nevertheless strengthen a frame without proving endorsement.

## Components

- `F1` — problem-definition salience;
- `F2` — agency, causality and responsibility organization;
- `F3` — moral evaluation and legitimacy;
- `F4` — treatment recommendation or strategic direction;
- `F5` — structural and rhetorical reinforcement.

The authoritative Hebrew ladders are in `CODEBOOK_DRAFT.md` version 0.4. A
missing treatment recommendation is `F4=0`, not `N/A`. Substantive `N/A` is
not used; an incomplete or uninspectable item is marked through
coverage and review status.

## Aggregation

All components have equal weight:

```text
F = 100 × (F1 + F2 + F3 + F4 + F5) / 20
```

The component vector, median, range, maximum, language, phase and publisher
breakdowns must accompany every case-level mean. `F` must never be inverted or
described as quality, truthfulness or bias.

## Evidence contract

Every positive component cites one or more cue IDs; a component scored `0` may
use an empty evidence list because no positive frame cue is being asserted.
Every cue must preserve an exact verbatim substring from the frozen title or text. Non-Hebrew cues require a
Hebrew reviewer translation that preserves attribution, uncertainty, register
and geographic meaning. The translation is a reviewer aid and never replaces
the original evidence.

Scores `F3` or `F4` of 3–4 require prominent evidence that is either
newsroom-owned or structurally foregrounded by the outlet. A severe score may
not rest only on an isolated attributed body quotation.

## Review routing

After the first proposal, an independent second pass reviews the full item and
a methodological counterargument. Human review is required when:

- any component differs by at least two points between passes;
- decisive cue ownership remains disputed;
- decisive translation remains uncertain;
- material visual content is unavailable;
- a high `F3` or `F4` lacks qualifying prominent evidence;
- exact evidence cannot be resolved to the frozen item.

CASE-002 supplied the nine-document calibration set and the first complete
27-item application. The identical contract is applied to CASE-001 and all
subsequent approved cases. Later cases do not reopen the construct or its
interpretation unless the researcher explicitly creates a new methodology
version.

Full F coverage for the four closed cases was verified at 108/108 articles in
`analysis/frame_intensity_coverage_audit_v1.json`. No rerun is required:
CASE-001 75.3704; CASE-002 79.8148; CASE-003 68.8889; CASE-004 76.4815.

## Versioning

Version 0.1.0 outputs use:

- methodology version `frame-intensity-v0.1.0`;
- prompt version `case002-frame-intensity-v0.1.0`;
- schema version `0.1.0`.

Version 0.2.0 changes only the interpretation and reporting contract, not the
F ladders or stored values. Any future change to a ladder, component, ownership
rule, aggregation rule or review threshold requires a new measurement version
and a documented rerun. Existing `T/N/A` artifacts remain immutable; `A/N`
remain available only for legacy audit.
