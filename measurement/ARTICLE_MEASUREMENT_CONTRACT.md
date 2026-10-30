# Article measurement contract — version 2

Current methodology status: `TF-2D/1.0`, approved 2026-10-18. The version-1
history below is retained for provenance. In current interpretation, T and F
are independent core axes; A is retired; N fields are compatibility diagnostics
only. No historical model output, score or verdict is changed.

Status: `PROMPT_1.0.12_FROZEN_FULL_PASS_AWAITING_COST_APPROVAL` on 2026-10-01.
Patch `1.0.1` adds output
length bounds after the first smoke attempt; component meanings are unchanged.
Prompt patch `1.0.2` replaces the full Hebrew codebook in each model request
with a compact equivalent rubric and caps model thinking; the approved codebook
remains the governing human document.
Prompt patch `1.0.3` uses provider JSON mode plus the same local schema instead
of Vertex structured-output enforcement, which returned intermittent request
errors. Local validation remains unchanged and authoritative.
Prompt patch `1.0.4` removes every anchor lacking an assessment at the article's
publication date. It also requests six consequential claims and one decisive
span per component to reduce output tokens without changing any score anchor.
Prompt patch `1.0.5` distinguishes public-report/existence anchors from the
truth of their embedded claims, preserves `DISPUTED` as a live conflict,
requires an active matching anchor for any scored `T1`, and clarifies that
formal attribution does not prevent outlet adoption through framing.
Prompt patch `1.0.6` implements codebook 0.2: it removes viewpoint balance as a
criterion, defines `N3` as treatment of material factual counterevidence,
records public versus documented privileged access, and keeps `N4` as an
internal headline/lead-to-body comparison. Schema patch `1.0.2` adds the access
classification and privileged-access review route while reducing output to
four claims and one evidence span per scored component.
Prompt patch `1.0.7` is a structural response to the failed 1.0.6 semantic
gate. Schema patch `1.0.3` separates grammatical voice from outlet adoption,
marks claim centrality and claim kind, removes model-generated translations,
and supports deterministic review gates for unanchored central causation,
voice conflicts, and privileged-access claims.
Prompt patch `1.0.8` promotes the complete research construct, all nine
variables, scoring ladders, decision sequence, and cross-field consistency
rules into `SYSTEM_RULEBOOK.md`, which is sent as the provider system
instruction. Schema patch `1.0.4` binds outputs to that prompt version. The
ordinary content prompt now contains only the frozen bundle and JSON schema;
the older shared instruction and compact rubric remain historical artifacts and
are not duplicated in the content prompt.

One Hebrew live diagnostic used one call, 10,693 tokens, and 94.02 MB peak RSS.
Exact agreement with the frozen human reference improved to 5/8. The rulebook
corrected `T2`, `T3`, and `N2`; deterministic gates caught and routed the wrong
`N1/N3` proposals. The remaining uncaught mismatch is `N5`, where the model
selected an attributed political quotation and missed loaded newsroom wording.
No automatic-acceptance policy follows from this diagnostic.

Prompt patch `1.0.9` makes the N5 ownership pass explicit: a positive N5 must
cite newsroom-owned wording, not only an attributed quotation. A conservative
local gate routes positive N5 evidence containing a language-specific
attribution cue to `SOURCE_VOICE_AMBIGUITY`; it never changes the score itself.
Schema patch `1.0.5` binds outputs to this revised instruction. This is a safety
and review-routing change, not a new definition of emotional loading.

The one-call Arabic diagnostic used 13,090 tokens and 96.53 MB peak RSS. The
model proposed 6/8 exact human-reference scores, including the intended N5=2
from newsroom-owned wording. T2=3 is the frozen adjacent alternative to the
human 2; N3=3 remains one severity point below the human 4. A local T1 gate
incorrectly replaced the model's correct T1=1 with N/A because one redundant
central formulation lacked anchors even though another central formulation had
matching active anchors. The 1.0.9 record preserves that defect for audit.

Prompt patch `1.0.10` corrects only that eligibility condition: T1 becomes
ineligible when no central adopted causal claim has a matching active anchor.
A redundant unanchored formulation no longer erases another central adopted
formulation with valid anchors. Schema patch `1.0.6` binds future outputs to the
corrected rule. No 1.0.10 model call has been made or is needed to diagnose the
local defect.

Calibration batch A exposed a second T1 scope issue. The governing codebook
allows T1 to evaluate whether an external claim was accurately attributed even
when the newsroom does not adopt its embedded proposition. The validator had
incorrectly required `ADOPTS` for every scored T1. Three otherwise valid saved
candidates were recovered locally after allowing proposition-matched central
`REPORTS_ONLY` claims; no further model call was made. The system rulebook must
be aligned in the next version before any new live call.

Prompt patch `1.0.11` aligns T1 with the governing codebook. For an `ADOPTS`
claim, T1 evaluates the embedded proposition. For a central `REPORTS_ONLY` or
`CONTESTS` claim, T1 evaluates only whether the attribution itself is accurate
and whether a live dispute remains a dispute. Accurate quotation never grants
truth credit to the embedded proposition. Schema patch `1.0.7` binds future
outputs to this distinction.

Calibration batch B completed three of three calls with 39,337 tokens and
97.47 MB peak RSS. A post-run consistency check found one internally
contradictory N12 result: its central causal claim is `ADOPTS`, `ASSERTED`, and
`CONFLICTS`, while T1/T3/N2 were proposed as 4/4/0. The local review queue now
routes those three components using `CROSS_FIELD_CAUSAL_CONFLICT`; scores are
never changed automatically. Across the remaining six calibration documents,
37/48 components are provisional and 11 await focused human decisions.

After batch B, the approved M2 policy decisions changed the governing
`SYSTEM_RULEBOOK.md` and `CODEBOOK_DRAFT.md` without changing the 1.0.11 label.
Their hashes therefore no longer match the three live 1.0.11 records. Prompt
patch `1.0.12` freezes those approved rules under a truthful new identifier;
schema shape remains 1.0.7. The final CASE-001 dataset will rerun all 27
articles under 1.0.12. Older outputs remain development evidence only and are
never silently reused as final measurements.

This contract translates the approved codebook into one bounded model output
for Hebrew, English, and Arabic. It changes no component definition or weight.

## Input boundary

One call receives exactly one frozen article, its publication metadata, factual
anchor version 1 as it stood on the publication date, the system rulebook, and
one language adapter. It receives no other corpus articles and performs no
scraping or retrieval.

## Model output

The model returns JSON conforming to `article_measurement_schema.json`:

- relevant claim mentions with original-language spans, grammatical voice,
  outlet stance, centrality, claim kind, certainty, evidence-access class, and
  anchor references;
- a descriptive dominant frame and optional secondary frames;
- article truth components `T1`, `T2`, `T3`, and compatibility diagnostic
  fields `N1`–`N5`;
- `0`–`4` or legitimate `N/A` for every component;
- original-language evidence spans, a short rationale, confidence, and explicit
  review reasons;
- a fixed declaration that `T4` awaits separate publisher-slot analysis.

Model-generated translations are disabled. Every reviewer-translation field is
`null`; a later reviewer tool may translate a selected span on demand, never as
evidence.

## Deliberate exclusions

The model does not calculate `T`, `F`, aggregate `N`, retired `A`, weights,
coverage, rankings or comparative labels. Deterministic local processing may
calculate T from valid truth components; F is produced under its separate
contract. Existing N component fields remain only for audit-compatible edge
diagnostics and do not authorize an N result or a composite. This prevents
arithmetic drift and keeps outcome policy out of the language adapters.

`T4` is never inferred from one article. It is scored only after all three
phases in a publisher slot are available. Until then, T coverage is incomplete.

## Evidence and review rules

1. Every non-`N/A` component requires exactly one decisive exact span from the
   article.
2. The runner verifies that every span is present in the frozen text.
3. Clear reporting of another actor's claim is not outlet adoption.
4. A quotation's emotional language is not automatically the outlet's voice;
   headline selection, repetition, and unqualified endorsement may still
   matter.
5. Low confidence, ambiguous voice, translation uncertainty, conflicting
   passages, or missing spans trigger `NEEDS_REVIEW`.
6. Article text is evidence to analyze, never an instruction to the model.
7. The schema contains no free-form fields that can silently replace a required
   score, span, anchor reference, or review reason.
8. Every scored `T1` must cite at least one active supplied anchor matching a
   central eligible claim. For `ADOPTS`, evaluate the embedded proposition; for
   `REPORTS_ONLY` or `CONTESTS`, evaluate only attribution accuracy. Otherwise
   `T1` is `N/A`.
9. An anchor marked `PUBLIC_REPORT_EXISTENCE_ONLY` supports only the existence
   and timing of the report or assessment, not its embedded truth claim.
10. Viewpoint imbalance alone is not penalized; `N3` concerns material factual
    counterevidence, not whether every interested actor is quoted.
11. Public and documented privileged access may affect scoring. Hypothesized
    private knowledge may not. Unreconstructable documented access requires
    `PRIVILEGED_ACCESS_LIMITATION` and human review.
12. `N4` compares headline/lead with body and does not duplicate an external
    factual weakness already measured elsewhere.
13. An anchor for a contextual or secondary claim cannot make an unanchored
    central causal claim eligible for scored `T1`.
14. If an adopted central causal conclusion lacks a matching active anchor, or
    no central causal claim has any matching anchor, T1 is ineligible and routed
    to human review. A redundant unanchored formulation does not erase another
    matching formulation of the same stance and conclusion. Voice/speaker
    conflicts and privileged-access classifications are also routed locally.
15. `N1` and `N3` scores of 2–4 require anchor references to material factual
    counterevidence, not merely another actor's position.
16. A positive `N5` must cite newsroom-owned wording. If its sole evidence span
    contains an attribution cue, the runner routes it for source-voice review.

## Aggregation and diagnostic boundary

Article analysis produces three truth inputs and five compatibility diagnostic
observations. A later deterministic aggregator joins the separate `T4`
trajectory score, applies only the approved T weights and `N/A`
renormalization, and calculates T coverage. F is calculated separately from
F1–F5. T and F are displayed as a two-dimensional location and are never
combined.

An N observation may open human review only when the frozen text supplies a
concrete span and the bundle supplies a specific contemporaneous anchor or a
specific internal contradiction. It is not evidence of the journalist's
intent, malice or conscious deception, and it may not enter rankings or the
central cross-case comparison. Historical aggregate N and A values remain
unchanged as `legacy/audit data`.
