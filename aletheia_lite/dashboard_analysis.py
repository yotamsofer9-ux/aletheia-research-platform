from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from .case_results import ScoreRecord


PHASES = ("INITIAL_REPORT", "EARLY_UPDATE", "LATER_INVESTIGATION")
COMPONENTS = ("T1", "T2", "T3", "F1", "F2", "F3", "F4", "F5")


@dataclass(frozen=True)
class DashboardDataset:
    articles: pd.DataFrame
    cases: pd.DataFrame
    slots: pd.DataFrame


def build_dashboard_dataset(
    case_bundles: Iterable[
        tuple[str, str, list[ScoreRecord], dict[str, Any]]
    ],
) -> DashboardDataset:
    """Materialize the active TF-2D dashboard dataset without legacy A/N.

    The dashboard is a read-only projection of closed-case result artifacts.
    T and F remain independent columns. Historical aggregate N and A values
    are intentionally not copied into the active analytical frames.
    """

    article_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    slot_rows: list[dict[str, Any]] = []

    for case_id, case_label, score_records, frame_payload in case_bundles:
        case_record = next(row for row in score_records if row.level == "CASE")
        frame_by_document = {
            row["document_id"]: row for row in frame_payload["records"]
        }
        case_rows.append(
            {
                "case_id": case_id,
                "case_label": case_label,
                "T": case_record.truth_score,
                "F": frame_payload["distribution"]["mean"],
                "T_coverage": case_record.truth_coverage,
                "critical_flags": tuple(case_record.critical_flags),
            }
        )

        for score in score_records:
            if score.level == "DOCUMENT":
                frame = frame_by_document[score.entity_id]
                truth_components = score.details.get("component_scores") or {}
                frame_components = frame.get("component_scores") or {}
                article_rows.append(
                    {
                        "case_id": case_id,
                        "case_label": case_label,
                        "document_id": score.entity_id,
                        "title": score.label,
                        "source": score.details["source_name"],
                        "publisher_slot": score.details["publisher_slot"],
                        "language": score.language,
                        "phase": score.details["phase"],
                        "T": score.truth_score,
                        "F": frame["frame_score"],
                        "T_coverage": score.truth_coverage,
                        "critical_flags": tuple(score.critical_flags),
                        "frame_families": tuple(frame.get("frame_families") or ()),
                        **{
                            component: truth_components.get(component)
                            for component in ("T1", "T2", "T3")
                        },
                        **{
                            component: frame_components.get(component)
                            for component in ("F1", "F2", "F3", "F4", "F5")
                        },
                    }
                )
            elif score.level == "PUBLISHER_SLOT":
                slot_rows.append(
                    {
                        "case_id": case_id,
                        "case_label": case_label,
                        "publisher_slot": score.entity_id,
                        "source": score.label,
                        "language": score.language,
                        "T": score.truth_score,
                        "T4": score.details["component_means"].get("T4"),
                        "T4_na_reason": score.details.get("t4_na_reason"),
                        "T_coverage": score.truth_coverage,
                    }
                )

    articles = pd.DataFrame(article_rows)
    cases = pd.DataFrame(case_rows)
    slots = pd.DataFrame(slot_rows)
    if articles.empty or cases.empty:
        raise ValueError("Dashboard requires at least one closed case")
    return DashboardDataset(articles=articles, cases=cases, slots=slots)


def dashboard_snapshot(dataset: DashboardDataset) -> dict[str, Any]:
    articles = dataset.articles
    return {
        "cases": int(len(dataset.cases)),
        "articles": int(len(articles)),
        "languages": articles["language"].value_counts().sort_index().to_dict(),
        "phases": articles["phase"].value_counts().sort_index().to_dict(),
        "complete_T": int(articles["T"].notna().sum()),
        "complete_F": int(articles["F"].notna().sum()),
        "mean_T_coverage": float(articles["T_coverage"].mean()),
        "article_pearson": float(articles[["T", "F"]].corr().iloc[0, 1]),
        "article_spearman": float(
            articles[["T", "F"]].corr(method="spearman").iloc[0, 1]
        ),
        "case_pearson": float(dataset.cases[["T", "F"]].corr().iloc[0, 1])
        if len(dataset.cases) > 1
        else None,
        "case_spearman": float(
            dataset.cases[["T", "F"]].corr(method="spearman").iloc[0, 1]
        )
        if len(dataset.cases) > 1
        else None,
    }


def paired_trajectory_deltas(dataset: DashboardDataset) -> pd.DataFrame:
    """Return initial-to-later change for each case/publisher trajectory."""

    wide = dataset.articles.pivot_table(
        index=["case_id", "case_label", "publisher_slot", "source", "language"],
        columns="phase",
        values=["T", "F"],
        aggfunc="first",
    )
    required = [
        (metric, phase)
        for metric in ("T", "F")
        for phase in ("INITIAL_REPORT", "LATER_INVESTIGATION")
    ]
    complete = wide.dropna(subset=required).copy()
    result = complete.reset_index()[
        ["case_id", "case_label", "publisher_slot", "source", "language"]
    ]
    result["delta_T"] = (
        complete[("T", "LATER_INVESTIGATION")]
        - complete[("T", "INITIAL_REPORT")]
    ).to_numpy()
    result["delta_F"] = (
        complete[("F", "LATER_INVESTIGATION")]
        - complete[("F", "INITIAL_REPORT")]
    ).to_numpy()
    return result


def case_centered_language_results(dataset: DashboardDataset) -> pd.DataFrame:
    """Summarize language strata relative to each case mean.

    Centering prevents the aggregate view from presenting language strata as
    if they were independent of case composition. The values remain
    descriptive because language and publisher ecology are still entangled.
    """

    grouped = (
        dataset.articles.groupby(["case_id", "case_label", "language"])[["T", "F"]]
        .mean()
        .reset_index()
    )
    for metric in ("T", "F"):
        grouped[f"delta_{metric}"] = grouped[metric] - grouped.groupby("case_id")[
            metric
        ].transform("mean")
    return grouped


def component_long_frame(dataset: DashboardDataset) -> pd.DataFrame:
    return dataset.articles.melt(
        id_vars=["case_id", "case_label", "document_id", "language", "phase"],
        value_vars=list(COMPONENTS),
        var_name="component",
        value_name="score",
    )


def frame_family_prevalence(dataset: DashboardDataset) -> pd.DataFrame:
    """Return multi-label article prevalence by language and frame family."""

    rows = []
    for article in dataset.articles.itertuples(index=False):
        for family in article.frame_families:
            rows.append(
                {
                    "case_id": article.case_id,
                    "document_id": article.document_id,
                    "language": article.language,
                    "family": family,
                }
            )
    exploded = pd.DataFrame(rows)
    if exploded.empty:
        return pd.DataFrame(columns=["family", "language", "articles", "share"])
    language_totals = dataset.articles.groupby("language")["document_id"].nunique()
    prevalence = (
        exploded.groupby(["family", "language"])["document_id"]
        .nunique()
        .rename("articles")
        .reset_index()
    )
    prevalence["share"] = prevalence.apply(
        lambda row: row["articles"] / language_totals[row["language"]], axis=1
    )
    return prevalence


def flagged_articles(dataset: DashboardDataset) -> pd.DataFrame:
    mask = dataset.articles["critical_flags"].map(bool)
    return dataset.articles.loc[
        mask,
        [
            "case_id",
            "case_label",
            "source",
            "language",
            "phase",
            "title",
            "T",
            "F",
            "critical_flags",
        ],
    ].reset_index(drop=True)
