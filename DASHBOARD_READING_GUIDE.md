# ALETHEIA dashboard reading guide — TF-2D/1.0

Status: active, approved methodology as of 2026-10-18.

## Primary display

Dashboards must present `T` and `F` as separate axes on a two-dimensional map.
They must not calculate, display or imply a combined score. `T` measures truth
fidelity: anchor alignment, source transparency, fidelity to uncertainty and
correction across time. `F` measures frame intensity: the salience, coherence
and organization of an interpretive frame.

`F` is not a measure of truth, quality, fairness, balance, bias, manipulation
or malice. A high F value is neither good nor bad. The research compares
observable truth fidelity with the construction of narrative order; it does
not attempt to infer what a journalist privately intended.

Every chart must expose the unit of analysis, case, language, phase, publisher,
sample size, coverage and methodology version. Hover or detail views should
show component vectors rather than only means.

## Reading the four regions

| Region | Permitted descriptive reading | Prohibited shortcut |
| --- | --- | --- |
| High T / High F | Strong fidelity to available evidence with strong narrative organization | “good framing,” neutrality, or lack of political consequence |
| High T / Low F | Strong fidelity with comparatively diffuse or weak narrative organization | automatic superiority, completeness or usefulness |
| Low T / High F | Strong narrative organization with weak fidelity to the defined evidence criteria | deliberate deception, propaganda, malice or conscious manipulation |
| Low T / Low F | Weak fidelity with diffuse or weak organization | motive; noise, incompleteness and genre constraints remain possible |

Quadrant boundaries must be preregistered or clearly marked as transparent
sample-relative reference lines. They may not be selected after inspecting a
favored result. Continuous coordinates and uncertainty should remain visible
even when regions are used as explanatory shorthand.

## N diagnostic tray

`N` is not a plotted core axis, outcome, ranking or case-comparison measure. A
separate diagnostic tray may show an N flag only if its record contains:

- an exact, inspectable text span and ownership/location;
- a specific contemporaneous anchor or exact internal contradiction;
- an explanation of why the departure is material;
- a human-review state and provenance.

The tray must label all unreviewed items `DIAGNOSTIC — NOT A VERDICT`. Even an
affirmed flag is a bounded textual finding and not proof of intent, malice,
bias or conscious deception. A flag does not change T or F automatically.

## Legacy panel

Historical A and aggregate-N values are retained to reproduce earlier runs.
If exposed, they must appear in a collapsed panel titled `LEGACY / AUDIT — NOT
A CURRENT RESEARCH OUTCOME`. They must not drive sorting, color scales,
rankings, headlines, comparisons or exports intended for substantive analysis.

## Verified closed-case F coverage

`analysis/frame_intensity_coverage_audit_v1.json` verifies complete F values
for 108/108 articles. The closed cases require no rerun:

| Case | Articles with complete F | Mean F |
| --- | ---: | ---: |
| CASE-001 | 27/27 | 75.3704 |
| CASE-002 | 27/27 | 79.8148 |
| CASE-003 | 27/27 | 68.8889 |
| CASE-004 | 27/27 | 76.4815 |

These means describe frame intensity only. Their order is not a quality or
truth ranking.

## Current pipeline status

CASE-005, CASE-006 and CASE-007 have passed Gate 1 and are in basket
construction while alternative sources await researcher decisions. None has
passed Gate 2, so the dashboards must not show them as analyzed or comparable
cases.
