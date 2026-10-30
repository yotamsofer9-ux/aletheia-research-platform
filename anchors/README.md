# CASE-001 factual anchors

This directory separates three things that the legacy fixture mixed together:

1. `case001_seed_audit.json` records how the 12 old seed claims were split or
   reframed. Its legacy labels are never ground truth.
2. `case001_evidence_matrix.json` records what each quality-screened source
   contributes, its limitations, publication date, and likely dependency group.
3. `case001_factual_anchors_draft.jsonl` contains 16 proposed factual anchors
   and their time-indexed assessments. This immutable input remains `DRAFT`;
   adjudicated statuses appear only in the versioned output.
4. `case001_adjudications_v1.jsonl` preserves the decision and provenance for
   every assessment: 21 exact Vertex approvals and one transparent human
   override.
5. `case001_factual_anchors_v1.jsonl` is the immutable materialized research
   instrument. It is generated from the draft, saved model reviews, and the
   approved PIJ override; none of those inputs is overwritten.

`anchor_record_schema.json` validates each JSONL row. A temporal reference keeps
both its value and precision so that a calendar date is not silently converted
into a fabricated midnight timestamp.

## Status and confidence

The epistemic status evaluates the proposition against evidence public by the
assessment date:

- `SUPPORTED`: the balance of available evidence supports the proposition;
- `CONTRADICTED`: the balance of available evidence weighs against it;
- `DISPUTED`: material positive and negative claims remain in active conflict;
- `UNRESOLVED`: the public record is insufficient for a directional judgment.

Confidence describes confidence in that classification, not confidence that an
institutional or media source is generally trustworthy. For example,
`UNRESOLVED/HIGH` means strong confidence that the question remained unresolved.

Sources do not become independent votes merely because several organizations
published analyses. AP, Le Monde, Bellingcat, Forensic Architecture, and parts
of HRW rely on overlapping public imagery. Western governmental assessments may
also rely on shared allied intelligence. Dependency groups prevent false source
counting.

Each evidence reference resolves by globally unique `source_id` across two
registries: `bibliography/research_sources.jsonl` for research-supporting and
primary material, and `bibliography/journalistic_sources.jsonl` for newsroom
reporting and journalistic investigations. The split controls bibliography
status; it does not make either registry automatically true or independent.

## Current draft synthesis

- High-confidence observations: courtyard/parking impact, small crater, main
  structure not completely destroyed, published official estimates and audio.
- Evidence-balance findings: a large air-dropped bomb is strongly disfavored;
  a Gaza-launched rocket-propelled munition is favored but not conclusively
  established.
- Low-confidence supported finding: PIJ-specific attribution, recorded as a
  transparent human weighting judgment rather than a model-verified fact.
- Unresolved: exact casualty count and audio authenticity/attribution.
- Contradicted: the strong claim that the U.S. assessment used only evidence
  independent of Israel.

The draft statements remain a review queue. Version 1 is the frozen research
instrument for calibration and retains all uncertainty and provenance.
