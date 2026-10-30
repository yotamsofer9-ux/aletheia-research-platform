# ALETHEIA judgment room

Methodology status: aligned to `TF-2D/1.0` on 2026-10-18. Historical
measurements and verdicts remain immutable. The room may display legacy A/N
fields for audit, but they are not current outcomes.

The judgment room is a local, low-memory Streamlit interface for the principal
researcher's decisions. It does not call Vertex and it never overwrites the
frozen model measurements.

Run it from the repository root:

```bash
streamlit run app.py
```

The initial courtroom contains the 11 historical `CASE-001` components already routed by
the hybrid acceptance policy. Every case contains the article metadata and
text, decisive span, Vertex score and rationale, a substantive methodological
counterargument, the scoring ladder, and relevant factual anchors.

A courtroom exhibit is distinct from a score-eligible anchor. A headline or
other central claim may be restored as a curated exhibit when a deterministic
gate removed the model's evidence span. The room then shows every relevant
anchor with a visible role: formally matched, contemporaneous context only,
proposition-matched but later, or a later competing hypothesis. Later and
context-only anchors remain useful to the judge but do not silently become
publication-time evidence.

For a decisive exhibit in Arabic or English, the courtroom shows a stored
Hebrew research translation as the primary text. The exact source-language
span remains available immediately below it, together with a compact glossary
for geographic names, register, ambiguity, and politically or legally loaded
terms. Translations are joined by document ID and exact source text. A future
non-Hebrew exhibit without a matching translation is visibly blocked from
adjudication rather than silently passed through or translated at runtime.

Visual evidence is curated separately in
`calibration/case001_visual_exhibits_v1.json`. The courtroom streams each
publisher-hosted video directly and offers a poster-frame fallback, so no video
binary is copied into SQLite or loaded into the Python process. Every visual
exhibit includes a Hebrew caption, the original publisher caption, frozen
snapshot provenance, and two explicit scope statements: what the exhibit can
support and what it cannot establish. The exact exhibit records shown to the
judge are copied into the versioned decision snapshot when a verdict is saved.
The player uses the publisher's poster as its visible initial frame and also
offers a direct-media link because the underlying night footage can otherwise
look like an empty black player before playback begins.

## Decision semantics

- `ACCEPT_MODEL` makes the model proposal the effective component result.
- `ACCEPT_CHALLENGE` makes the displayed counterargument the effective result.
- `CUSTOM` records an independent score from 0–4 or an explicit N/A.
- `DEFER` keeps the component pending and gives it no effective score.

Each save appends a new version to `data/judgment_room.sqlite3`. A revised
decision marks the previous version historical but does not delete it. The
effective-results tab joins the current human decisions onto the frozen model
queue at read time. The remaining model results stay provisional. The room
never calculates or publishes a composite Aletheia score.

## Current result and review semantics

The central research output is a two-dimensional `T × F` position. `T` reports
truth fidelity; `F` reports only the salience, consistency and organization of
an interpretive frame. High F is neither good nor bad and is not a measure of
truth, quality, bias or malice. No control may combine, invert or rank the two
axes as one number.

The room studies the relation between truth and the construction of narrative
order through observable evidence. It does not try to read the journalist's
mind. An N item may appear only in a separate diagnostic docket and only when
it identifies:

1. an exact text span and its ownership or structural placement;
2. a specific contemporaneous anchor or exact internal contradiction;
3. a material departure rather than stylistic disagreement;
4. the reason human review is needed.

The permitted decisions are to affirm the bounded textual flag, reject it, or
defer for missing evidence. Even an affirmed flag cannot be described as proof
of intent, malice or deliberate deception and cannot alter T or F
automatically, enter a summary score, rank a source or drive the central
cross-case comparison.

Historical A and aggregate-N fields remain visible only in an explicitly
labelled `LEGACY / AUDIT` view. Their provenance, model proposals and human
verdicts remain append-only.

The current decisions can be downloaded from the interface as JSONL. The local
SQLite schema is intentionally narrow and can later be mirrored into BigQuery
without changing the courtroom contract.

## Component score ledger

`component_score_ledger` is the research audit table for scoring reliability.
It stores the 24 frozen human-reference scores from the smoke set, one
immutable model-proposal record for each of the 48 later measured
document-component pairs, and one record for every version of a courtroom
verdict. Each record contains the score or explicit N/A reason, the rationale,
evidence span, anchor references, prompt or codebook version, authority,
version, reviewer, and current/effective flags.

The courtroom's “score and rationale table” presents one current row for each
of the 72 article-level calibration components. It shows the model proposal and
its reason beside the current research result and its controlling reason.
Pending human cases remain visibly unscored. `T4` is explicitly excluded from
this count because it requires a same-publisher three-phase trajectory. The
full ledger keeps superseded verdicts and is downloadable as CSV; a new
judgment never overwrites a frozen reference, model record, or earlier human
version.

The 72 rows cover nine calibration articles: three Hebrew, three English, and
three Arabic. The full frozen corpus has 27 articles, so 18 remain unanalyzed —
six in each language stratum. If the M2 gate approves expansion, those articles
will add 144 article-level rows before the separate T4 trajectory records.

## Knowledge preservation and precedent

Every verdict also records how it may influence later cases:

- `CASE_ONLY` remains in the audit log but is not proposed in future cases.
- `PERSUASIVE_PRECEDENT` is the default and is shown in similar dilemmas.
- `BINDING_POLICY` is shown first and requires the judge to write an explicit
  general rule.

Similarity is deterministic and inspectable: the component must match and the
cases must share at least one issue tag. No embedding model or additional LLM
is loaded. A matched precedent is advisory; it never saves or changes a verdict
automatically. Conflicting outcomes and known conflicts with the frozen
rulebook are displayed for human resolution.

The ledger stores `precedent_mode`, a reusable `policy_rule`, the issue-tag
scope in `policy_scope_json`, and a metadata version. If the judge leaves the
rule field blank for a non-binding verdict, the system derives a rule from the
chosen outcome and written rationale so the stored rule cannot accidentally
describe the rejected side. Revisions preserve history and only the current
decision remains an active precedent.

Cross-case policy is stored separately in
`calibration/case001_policy_decisions_v1.json`. This prevents a general policy
decision from silently rewriting the verdict that generated it. The registry
records the approved choice, written rule, affected cases, precedent-scope
overrides, and conflict dispositions. The app applies those overrides when it
builds the active precedent library: a case-only result remains in the score
ledger but is not offered as future guidance, and a conflict is hidden only
when an explicit human policy marks that exact pair resolved by scope.

The final CASE-001 policy application preserves the N12 `T1=4`, `T3=4`,
`N2=0` triplet as three new append-only human versions. They have revised
rationales and remain effective for the article, but policy override
`CASE001-POL-APP-D3-N12-N2` marks all three `CASE_ONLY`. They therefore appear
in the ledger and resolved-policy display but not in the active precedent
library.
