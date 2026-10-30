# Compact model rubric — CASE-001

> **Historical prompt artifact.** Preserved exactly for reconstruction of
> CASE-001. It is superseded prospectively by `SYSTEM_RULEBOOK.md` 2.0.0 and
> must not be read as making N a current core outcome.

Use the article's publication-time knowledge state. Score only the outlet's own
presentation; a clearly attributed claim is not automatically outlet adoption.
Every score needs a short exact original-language span. `N/A` is permitted only
when the component genuinely does not apply, never when the score is uncertain.

Anchor status applies only to the exact canonical proposition. A
`PUBLIC_REPORT_EXISTENCE_ONLY` anchor proves that a report or assessment was
public, not that its embedded factual or causal conclusion was true.
`DISPUTED` is a live material conflict, not `CONTRADICTED` or settled fact.
Attribution is evidence about voice, but headlines, narration, repetition,
loaded dismissal, and structure can still show outlet adoption.

Truth components use `4 = best`; narrative-pressure components use `4 = worst`.

## T1 — claim alignment

- 4: all central adopted claims align with available knowledge; disputes remain disputes.
- 3: central account aligns; one minor inaccuracy does not change understanding.
- 2: mixed; a material claim is weak or outdated but not clearly contradicted.
- 1: a central adopted claim conflicts with material available evidence, or a disputed claim is effectively stated as fact.
- 0: the central account severely conflicts with available knowledge or presents a fabricated/refuted claim as fact.
- N/A: no relevant factual claim can be compared with an available anchor.

A scored T1 must cite an active supplied anchor matching a central adopted
claim. A categorical statement of a `DISPUTED` claim will normally score 1;
reserve 0 for severe conflict, material refutation, or fabrication. If no active
anchor matches the central adopted claim, use N/A rather than assuming it is
true or false. Never use an anchor for a secondary casualty, report-existence,
or contextual claim to score an unanchored central causal claim.

Accurately saying “Actor X claimed Y” is evaluated as a report of X's claim,
not as adoption of Y.

## T2 — source and evidence transparency

- 4: central claims have identifiable sources/evidence and material limitations are visible.
- 3: most material claims are clearly attributable; only a small traceability gap remains.
- 2: attribution is mixed, indirect, or partly vague.
- 1: the central claim relies on vague reports or authority and is barely traceable.
- 0: false attribution, source laundering/invention, or an unsupported central claim presented as the outlet's knowledge.
- N/A: no external factual claim requiring attribution.

Anonymous sources are not automatically weak; consider described role,
proximity, multiplicity, explanation, and corroboration.

Classified raw material need not be disclosed. Naming the institution, its
competence, and the general evidence class may be sufficient. Vague reference
to “secret information” without a source or evidence class earns no automatic
credit.

Score 4 only when source identity, evidence class, the decisive reasoning
chain, and material limitations are sufficiently visible. Naming an official
source or the outlet's own investigative unit does not by itself earn 4.

## T3 — fidelity to uncertainty

- 4: known, alleged, assessed, and unknown are consistently distinguished.
- 3: calibration is mostly correct; one mild over/understatement is immaterial.
- 2: uncertainty is mentioned but a material dispute is compressed or blurred.
- 1: a central disputed claim is expressed with high certainty, or established information is misleadingly treated as mere speculation.
- 0: categorical certainty clearly contradicts the known uncertainty state.
- N/A: no relevant uncertainty-bearing claim.

## N1 — selective salience or material omission

- 0: material evidence receives proportionate attention for the article's scope.
- 1: mild imbalance, but the material picture remains available.
- 2: material counterevidence is brief or marginal, creating noticeable skew.
- 3: a central available limitation/counterevidence is omitted or buried, materially changing understanding.
- 4: systematic one-sided selection repeatedly foregrounds support and hides central contrary information.
- N/A: no material counterevidence was available, or article scope creates no reasonable expectation to include it.

Do not penalize failure to mention every anchor. Omission counts only when the
information was available, material to the central claim, and appropriate to
the article's genre and scope. Viewpoint imbalance alone is not evidentiary
omission.

## N2 — premature causal closure or responsibility

- 0: causation/responsibility matches evidence, with attribution and caveats.
- 1: wording or structure implies slightly more than evidence supports.
- 2: a noticeable causal conclusion is created while the basis remains partial.
- 3: responsibility is stated firmly despite a material live dispute and competing evidence.
- 4: categorical blame conflicts with strong available evidence or continues after material refutation.
- N/A: the article does not address cause or responsibility.

Categorical blame while the active anchor is `DISPUTED` normally scores 3, not
4. Score 4 only when strong contrary evidence or material refutation was
already available.

“Actor X blamed Y” alone is not outlet causal closure.

Evidence includes public information and documented privileged access. Never
reward or punish the outlet based on hypothesized private knowledge. If access
is documented but its content cannot be reconstructed, route
`PRIVILEGED_ACCESS_LIMITATION` for human review.

## N3 — material counterevidence

- 0: no material counterevidence was omitted or distorted; no duty exists to quote every side.
- 1: minor counterevidence is underdeveloped, but its meaning remains understandable.
- 2: material counterevidence is mentioned but buried, compressed, or judged by a substantially harsher standard than supporting evidence.
- 3: central material counterevidence available to the outlet is omitted from an article advancing a central conclusion.
- 4: central counterevidence is distorted, suppressed, or presented misleadingly to make the account appear unassailable.
- N/A: the article advances no conclusion for which relevant counterevidence can be identified.

An interested actor's claim, denial, or position is not itself counterevidence.
Viewpoint balance is not the measure. Scores 2–4 must cite supplied anchors for
the factual counterevidence being buried, omitted, distorted, or suppressed.

## N4 — headline/lead versus body

- 0: headline and lead preserve the body's central evidence, attribution, and caveats.
- 1: harmless simplification.
- 2: headline/lead is noticeably stronger in certainty, blame, or emotion.
- 3: body caveats materially undermine the headline's central impression.
- 4: headline/lead states as fact a central claim the body does not support or contradicts.
- N/A: original headline or lead is unavailable.

This is an internal comparison. External factual error belongs in T1, T3,
N2, or N3 and must not be counted again unless the headline/lead is materially
stronger than the body itself.

## N5 — disproportionate emotional or moral loading

- 0: descriptive and proportionate editorial language.
- 1: limited justified emotional language that does not distort the account.
- 2: repeated loaded wording steers judgment while evidence/caveats remain accessible.
- 3: severe moral labeling or demonization largely substitutes for evidence or uncertainty.
- 4: extreme sensationalism, dehumanization, incitement, or collective blame dominates.
- N/A: only a technical/non-editorial fragment is available; ordinary neutral reporting scores 0, not N/A.

Attributed emotional quotation is not automatically outlet language. Headline
selection, repetition, or unqualified endorsement can make it editorially
relevant.

## Review routing

Use low confidence plus `LOW_CONFIDENCE` review routing when a score is
uncertain. Also route ambiguous source voice, translation uncertainty,
conflicting passages, missing exact spans, anchor ambiguity, or documented
privileged access whose content cannot be reconstructed. Never calculate T, N,
A, weights, coverage, labels, or T4.
