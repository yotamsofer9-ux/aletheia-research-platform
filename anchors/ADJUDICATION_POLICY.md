# Factual-anchor adjudication policy

Version: 0.1

The factual-anchor draft, Vertex review, and final adjudication are separate
artifacts. No layer overwrites another.

## Outcomes

- `MODEL_APPROVED`: the current assessment matches a valid, current Vertex
  review. This is not represented as human approval.
- `HUMAN_OVERRIDE`: the researcher deliberately selects a different result.
  The record must preserve the model review and classify the override basis as
  public inspectable evidence, non-public knowledge, or methodological judgment.
- `NEEDS_MORE_EVIDENCE`: the assessment cannot responsibly enter anchor version
  1 yet. No epistemic status or confidence is assigned by the adjudication.
- `EXCLUDED`: the proposition is removed from the scored anchor set, with a
  rationale. Exclusion cannot be used to hide inconvenient evidence.

## Rules

1. A decision is made at assessment level, because the same proposition may
   have different knowledge states at different dates.
2. A model approval is usable only when its input hash matches the current
   anchor and evidence bundle.
3. A human override never deletes or edits the model record it overrides.
4. `NON_PUBLIC_KNOWLEDGE` may document researcher judgment but must not be
   described as publicly reproducible evidence.
5. `NEEDS_MORE_EVIDENCE` and `EXCLUDED` produce no resulting status or
   confidence.
6. Public evidence must resolve to the research or journalistic registry by
   globally unique source ID.
7. Final anchor version 1 is materialized from adjudication records; the draft
   and review files remain immutable audit inputs.

## CASE-001 transition

Completed on 2026-09-29. Twenty-one assessments are `MODEL_APPROVED` against
their exact saved reviews. `CASE001-ACTOR-002-A1` is a documented
`HUMAN_OVERRIDE` to `SUPPORTED / LOW`, based on methodological weighting of the
public record. The original `UNRESOLVED / HIGH` model recommendation is
preserved. All 22 assessments have an outcome and factual-anchor version 1 is
materialized without editing the draft or model outputs.
