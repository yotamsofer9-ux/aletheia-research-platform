"""ALETHEIA — local human judgment room."""

from __future__ import annotations

import csv
import html
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from aletheia_lite.basket_progress import (
    load_run_state,
    run_is_active,
    save_run_state,
)

from aletheia_lite.case_results import (
    PHASE_LABELS_HE as RESULT_PHASE_LABELS_HE,
    ScoreRecord,
    build_scoring_readiness,
    build_t4_trajectories,
)
from aletheia_lite.case_generator_ui import (
    approve_topic_candidate,
    decide_article_basket,
    reject_topic_candidate,
    replacement_slot_decision_copy,
)
from aletheia_lite.case_registry import CaseRegistry, CaseRegistryError

from aletheia_lite.judgment_room import (
    COMPONENT_LABELS,
    ISSUE_TAGS_HE,
    SCORE_LADDERS,
    JudgmentStore,
    apply_staged_court_navigation,
    build_effective_rows,
    build_precedent_records,
    decision_status,
    decisions_as_jsonl,
    find_precedent_conflicts,
    find_relevant_precedents,
    load_case_material,
    load_policy_registry,
    next_actionable_dilemma_id,
    stage_court_navigation,
    split_dilemmas_by_status,
)
from aletheia_lite.research_workspace import (
    DOCUMENT_STATUS_HE,
    LANGUAGE_LABELS_HE,
    build_deterministic_queue,
    build_workspace_overview,
    current_peak_rss_mb,
    workspace_input_signature,
    workspace_sync_status,
)


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "data/judgment_room.sqlite3"
CASE_REGISTRY_PATH = BASE_DIR / "data/case_registry.sqlite3"
CASE2_REVIEW_PATH = (
    BASE_DIR / "data/cases/case-002/adjudication/principal_review_v1.json"
)
CASE2_DATABASE_PATH = (
    BASE_DIR / "data/cases/case-002/adjudication/judgment_room.sqlite3"
)
CASE3_REVIEW_PATH = (
    BASE_DIR / "data/cases/case-003/adjudication/principal_review_v1.json"
)
CASE3_DATABASE_PATH = (
    BASE_DIR / "data/cases/case-003/adjudication/judgment_room.sqlite3"
)
CASE4_REVIEW_PATH = (
    BASE_DIR / "data/cases/case-004/adjudication/principal_review_v1.json"
)
CASE4_DATABASE_PATH = (
    BASE_DIR / "data/cases/case-004/adjudication/judgment_room.sqlite3"
)
CASE5_REVIEW_PATH = (
    BASE_DIR / "data/cases/case-005/adjudication/principal_review_v1.json"
)
CASE5_DATABASE_PATH = (
    BASE_DIR / "data/cases/case-005/adjudication/judgment_room.sqlite3"
)
CASE6_REVIEW_PATH = (
    BASE_DIR / "data/cases/case-006/adjudication/principal_review_v1.json"
)
CASE6_DATABASE_PATH = (
    BASE_DIR / "data/cases/case-006/adjudication/judgment_room.sqlite3"
)
CASE7_REVIEW_PATH = (
    BASE_DIR / "data/cases/case-007/adjudication/principal_review_v1.json"
)
CASE7_DATABASE_PATH = (
    BASE_DIR / "data/cases/case-007/adjudication/judgment_room.sqlite3"
)
CASE8_REVIEW_PATH = (
    BASE_DIR / "data/cases/case-008/adjudication/principal_review_v1.json"
)
CASE8_DATABASE_PATH = (
    BASE_DIR / "data/cases/case-008/adjudication/judgment_room.sqlite3"
)
CASE1_RESULTS_PATH = BASE_DIR / "analysis/case001_final_results_v1.json"
CASE2_RESULTS_PATH = BASE_DIR / "data/cases/case-002/results/final_results_v1.json"
CASE3_RESULTS_PATH = BASE_DIR / "data/cases/case-003/results/final_results_v1.json"
CASE4_RESULTS_PATH = BASE_DIR / "data/cases/case-004/results/final_results_v1.json"
CASE5_RESULTS_PATH = BASE_DIR / "data/cases/case-005/results/final_results_v1.json"
CASE6_RESULTS_PATH = BASE_DIR / "data/cases/case-006/results/final_results_v1.json"
CASE7_RESULTS_PATH = BASE_DIR / "data/cases/case-007/results/final_results_v1.json"
CASE8_RESULTS_PATH = BASE_DIR / "data/cases/case-008/results/final_results_v1.json"
CASE3_CLOSEOUT_PATH = BASE_DIR / "data/cases/case-003/results/closeout_v1.json"
CASE4_CLOSEOUT_PATH = BASE_DIR / "data/cases/case-004/results/closeout_v1.json"
CASE5_CLOSEOUT_PATH = BASE_DIR / "data/cases/case-005/results/closeout_v1.json"
CASE6_CLOSEOUT_PATH = BASE_DIR / "data/cases/case-006/results/closeout_v1.json"
CASE7_CLOSEOUT_PATH = BASE_DIR / "data/cases/case-007/results/closeout_v1.json"
CASE8_CLOSEOUT_PATH = BASE_DIR / "data/cases/case-008/results/closeout_v1.json"
CASE5_ARTICLE_SUMMARY_PATH = (
    BASE_DIR / "data/cases/case-005/analysis/article_measurements_v1_summary.json"
)
CASE5_FRAME_SECOND_SUMMARY_PATH = (
    BASE_DIR
    / "data/cases/case-005/analysis/frame_intensity_full_second_pass_v1_summary.json"
)
CASE1_FRAME_RESULTS_PATH = BASE_DIR / "analysis/case001_frame_intensity_results_v1.json"
CASE2_FRAME_RESULTS_PATH = (
    BASE_DIR / "data/cases/case-002/results/frame_intensity_results_v1.json"
)
CASE3_FRAME_RESULTS_PATH = (
    BASE_DIR / "data/cases/case-003/results/frame_intensity_results_v1.json"
)
CASE4_FRAME_RESULTS_PATH = (
    BASE_DIR / "data/cases/case-004/results/frame_intensity_results_v1.json"
)
CASE5_FRAME_RESULTS_PATH = (
    BASE_DIR / "data/cases/case-005/results/frame_intensity_results_v1.json"
)
CASE6_FRAME_RESULTS_PATH = (
    BASE_DIR / "data/cases/case-006/results/frame_intensity_results_v1.json"
)
CASE7_FRAME_RESULTS_PATH = (
    BASE_DIR / "data/cases/case-007/results/frame_intensity_results_v1.json"
)
CASE8_FRAME_RESULTS_PATH = (
    BASE_DIR / "data/cases/case-008/results/frame_intensity_results_v1.json"
)
POLICY_PATH = BASE_DIR / "calibration/case001_policy_decisions_v1.json"
POLICY_APPLICATION_PATH = (
    BASE_DIR / "calibration/case001_policy_application_dilemma_d3_v1.json"
)

STATUS_HE = {"PENDING": "ממתין", "DEFERRED": "נדחה להמשך", "DECIDED": "הוכרע"}
OUTCOME_HE = {
    "ACCEPT_MODEL": "התקבלה טענת Vertex",
    "ACCEPT_CHALLENGE": "התקבלה ההשגה המתודולוגית",
    "CUSTOM": "פסק דין עצמאי",
    "DEFER": "הדיון נדחה",
}
PHASE_HE = {
    "INITIAL_REPORT": "דיווח ראשוני",
    "EARLY_UPDATE": "עדכון מוקדם",
    "LATER_INVESTIGATION": "בדיקה מאוחרת",
}
BASKET_SLOT_HE = {
    "domestic-israel-hayom": "ישראל היום",
    "domestic-zman-yisrael": "זמן ישראל",
    "domestic-channel13": "חדשות 13",
    "western-guardian": "The Guardian",
    "western-associated-press": "Associated Press",
    "western-new-york-times": "The New York Times",
    "regional-al-jazeera-arabic": "אל־ג׳זירה בערבית",
    "regional-cnn-arabic": "CNN Arabic",
    "regional-al-araby-tv": "אל־ערבי",
}
TRANSLATION_STATUS_HE = {
    "RESEARCH_DRAFT": "טיוטת מחקר לביקורת לשונית",
    "MODEL_DRAFT_FOR_HUMAN_ADJUDICATION": "טיוטת מכונה לביקורת השופט",
}
PRECEDENT_MODE_HE = {
    "CASE_ONLY": "מקרה נקודתי",
    "PERSUASIVE_PRECEDENT": "תקדים מנחה",
    "BINDING_POLICY": "מדיניות מחייבת",
}
ANCHOR_ROLE_HE = {
    "FORMAL_MATCH": "עוגן תואם וכשיר לניקוד",
    "CONTEXT_ONLY_AVAILABLE": "זמין בזמן הפרסום — הקשרי בלבד",
    "PROPOSITION_MATCH_BUT_LATER": "תואם לטענה — אך מאוחר לפרסום",
    "COMPETING_HYPOTHESIS_BUT_LATER": "השערה מתחרה — אך מאוחרת לפרסום",
}

DASHBOARD_COLORS = {
    "ink": "#172033",
    "muted": "#526070",
    "grid": "#D9E1EA",
    "plot": "#F7F9FC",
    "truth": "#176B68",
    "coverage": "#6D5A8D",
    "blue": "#4776A5",
    "coral": "#C56B59",
    "plum": "#775A8B",
}
SLOT_COLORS = [
    DASHBOARD_COLORS["truth"],
    DASHBOARD_COLORS["blue"],
    DASHBOARD_COLORS["plum"],
]
CASE_COLORS = {
    "CASE-001": DASHBOARD_COLORS["blue"],
    "CASE-002": DASHBOARD_COLORS["coral"],
    "CASE-003": DASHBOARD_COLORS["truth"],
    "CASE-004": DASHBOARD_COLORS["plum"],
    "CASE-005": "#8B6A3D",
    "CASE-008": "#2F6F8F",
}
HEATMAP_COLORSCALE = [
    [0.0, "#7D3F4E"],
    [0.5, "#DCE3EA"],
    [1.0, "#176B68"],
]


st.set_page_config(
    page_title="ALETHEIA | חדר השיפוט",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(
    """
    <style>
      h1, h2, h3, h4, h5, h6, p, label,
      [data-testid="stMarkdownContainer"], [data-testid="stCaptionContainer"],
      [data-testid="stMetric"], [data-testid="stAlertContainer"] {
        direction: rtl; text-align: right;
      }
      input, textarea { direction: rtl; text-align: right; }
      [data-testid="stSidebar"] { border-left: 1px solid #d9d4c8; }
      .block-container { max-width: 1280px; padding-top: 2rem; }
      .case-meta { color: inherit; opacity: .72; font-size: .92rem; margin-bottom: 1rem; }
      .evidence {
        border-right: 4px solid #a96b35; background: #f8f4ec;
        padding: 1rem 1.2rem; border-radius: .35rem; line-height: 1.75; color: #302a20;
      }
      .pending-note {
        background: #fff7dc; border: 1px solid #e2c96d; padding: .75rem 1rem;
        border-radius: .4rem; margin-bottom: 1rem; color: #302a20;
      }
      .translation {
        border-right: 4px solid #4c7f72; background: #edf6f2;
        padding: 1rem 1.2rem; border-radius: .35rem; line-height: 1.8; color: #18352e;
      }
      div[data-testid="stMetric"] { border: 1px solid #e1ddd4; padding: .8rem; border-radius: .5rem; }
      .stButton button, .stDownloadButton button { width: 100%; }
      .dashboard-metric-grid {
        display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: .6rem; margin: .6rem 0 .55rem; direction: rtl;
      }
      .dashboard-metric-card {
        min-width: 0; min-height: 94px; padding: .8rem .75rem .7rem;
        border: 1px solid rgba(151, 164, 181, .42);
        border-top: 4px solid var(--metric-accent);
        border-radius: 10px; background: rgba(151, 164, 181, .10);
        color: inherit;
      }
      .dashboard-metric-label {
        color: inherit; opacity: .72; font-size: .78rem; line-height: 1.25;
      }
      .dashboard-metric-value {
        margin: .38rem 0 0; color: inherit; font-size: 1.9rem;
        font-weight: 500; line-height: 1;
      }
      .dashboard-metric-note {
        margin: 0 0 .8rem; padding: .65rem .8rem; border-radius: 8px;
        border-right: 4px solid #4776a5; background: rgba(71, 118, 165, .13);
        color: inherit; line-height: 1.55; font-size: .84rem;
      }
      .dashboard-flag {
        margin: .7rem 0 1rem; padding: .8rem 1rem; border-radius: 9px;
        border: 1px solid rgba(166, 75, 91, .52); border-right: 5px solid #a64b5b;
        background: rgba(166, 75, 91, .14); color: inherit; line-height: 1.55;
      }
      .dashboard-explainer {
        margin: .45rem 0 1rem; padding: .8rem 1rem; border-radius: 9px;
        border-right: 4px solid #4776a5; background: rgba(71, 118, 165, .13);
        color: inherit; line-height: 1.65;
      }
      .calibration-article {
        direction: rtl; text-align: right; white-space: pre-wrap;
        max-height: 520px; overflow-y: auto; line-height: 1.75;
        padding: 1rem 1.15rem; border: 1px solid rgba(151, 164, 181, .45);
        border-radius: .55rem; background: rgba(247, 249, 252, .96);
        color: #172033;
      }
      .calibration-original {
        direction: ltr; text-align: left; opacity: .82; font-size: .9rem;
        padding-top: .35rem;
      }
      @media (max-width: 520px) {
        .dashboard-metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def case_material_input_signature():
    patterns = (
        "analysis/runs/case001_article_review_queue_v1*.json",
        "calibration/case001_judgment_dilemmas_v1*.json",
        "calibration/case001_visual_exhibits_v1*.json",
        "calibration/case001_evidence_translations_he_v1*.json",
    )
    paths = [
        BASE_DIR / "calibration/case001_smoke_human_reference_v1.json",
        BASE_DIR / "anchors/case001_factual_anchors_v1.jsonl",
        BASE_DIR / "exports/case001_research_pilot_27.csv",
    ]
    for pattern in patterns:
        paths.extend(sorted(BASE_DIR.glob(pattern)))
    return tuple(
        (str(path.relative_to(BASE_DIR)), path.stat().st_size, path.stat().st_mtime_ns)
        for path in paths
    )


@st.cache_data(show_spinner=False)
def get_material(input_signature):
    del input_signature
    return load_case_material(BASE_DIR)


@st.cache_data(show_spinner=False)
def get_policy_registry(policy_mtime_ns, application_mtime_ns):
    # The mtimes are cache keys; the loader itself reads the canonical files.
    del policy_mtime_ns, application_mtime_ns
    return load_policy_registry(BASE_DIR)


@st.cache_resource(show_spinner=False)
def get_store():
    return JudgmentStore(DATABASE_PATH)


@st.cache_resource(show_spinner=False)
def get_multicase_store(path: str):
    return JudgmentStore(Path(path))


@st.cache_data(show_spinner=False)
def get_case2_review(path: str, size: int, mtime_ns: int):
    del size, mtime_ns
    return json.loads(Path(path).read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False)
def get_score_records(path: str, size: int, mtime_ns: int):
    del size, mtime_ns
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload, [ScoreRecord(**row) for row in payload["records"]]


@st.cache_data(show_spinner=False)
def get_json_payload(path: str, size: int, mtime_ns: int):
    del size, mtime_ns
    return json.loads(Path(path).read_text(encoding="utf-8"))


@st.cache_resource(show_spinner=False)
def get_case_registry():
    return CaseRegistry(CASE_REGISTRY_PATH)


@st.cache_data(show_spinner=False)
def get_sync_status(input_signature):
    del input_signature
    return workspace_sync_status(BASE_DIR)


def run_sync_action(action):
    completed = subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts/sync_m4_workspace.py"), action],
        cwd=BASE_DIR,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=900,
    )
    if completed.returncode:
        message = completed.stderr.strip().splitlines()
        raise RuntimeError(message[-1] if message else "פעולת הסנכרון נכשלה")
    get_sync_status.clear()
    return completed.stdout


def start_basket_discovery_action(case_id):
    current = load_run_state(BASE_DIR, case_id)
    if run_is_active(current):
        return current
    run_id = f"BASKET-{case_id}-{uuid.uuid4().hex[:12].upper()}"
    queued = save_run_state(
        BASE_DIR,
        case_id,
        run_id,
        "QUEUED",
        stage="QUEUED",
        articles_staged=int((current or {}).get("articles_staged", 0)),
        attempted_urls=0,
        missing_cells=int((current or {}).get("missing_cells", 27)),
        total_units=27,
        message="ההרצה הוכנסה לתור ותתחיל ברקע.",
        started_at=None,
        cloud_writes=0,
        model_calls=0,
    )
    log_path = (
        BASE_DIR
        / "analysis"
        / "case_generator"
        / case_id.lower()
        / f"{run_id.lower()}.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_handle = log_path.open("ab")
        subprocess.Popen(
        [
            sys.executable,
            str(BASE_DIR / "scripts/run_basket_discovery.py"),
            "--case-id",
            case_id,
                "--run-id",
                run_id,
        ],
        cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
    except OSError as error:
        save_run_state(
            BASE_DIR,
            case_id,
            run_id,
            "FAILED",
            stage="FAILED",
            message=f"לא ניתן היה להתחיל את ההרצה: {error}",
            finished_at=None,
        )
        raise RuntimeError("לא ניתן היה להתחיל את גילוי סל הכתבות") from error
    return queued


def load_basket_discovery_report(case_id):
    report_path = (
        BASE_DIR
        / "analysis"
        / "case_generator"
        / case_id.lower()
        / "basket_discovery_v1.json"
    )
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_topic_relevance_audit(case_id):
    path = (
        BASE_DIR
        / "analysis"
        / "case_generator"
        / case_id.lower()
        / "topic_relevance_audit_v2.json"
    )
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@st.fragment(run_every=5)
def render_basket_run_status(case_id):
    state = load_run_state(BASE_DIR, case_id)
    if not state:
        st.caption("טרם נוצרה הרצת סל מתועדת לקייס זה.")
        return
    status_labels = {
        "QUEUED": "ממתין להתחלה",
        "RUNNING": "הריצה פעילה",
        "WAITING_RETRY": "הניסיון הסתיים — נדרשת השלמה",
        "AWAITING_HUMAN_DECISION": "נדרשת הכרעת מקור חלופי",
        "AWAITING_GATE_2": "הסל הושלם — ממתין לאישור שער 2",
        "NO_PROGRESS": "לא נמצאה השלמה חדשה",
        "FAILED": "הריצה נעצרה",
    }
    staged = int(state.get("articles_staged", 0))
    total = max(int(state.get("total_units", 27)), 1)
    active = run_is_active(state)
    state_a, state_b, state_c = st.columns(3)
    state_a.metric("מצב הריצה", status_labels.get(state.get("status"), state.get("status")))
    state_b.metric("כתבות תקינות", f"{staged}/{total}")
    state_c.metric("משבצות חסרות", int(state.get("missing_cells", total - staged)))
    st.progress(min(staged / total, 1.0))
    if active:
        st.info(state.get("message") or "ההרצה פעילה ברקע.")
        st.caption("התצוגה מתעדכנת אוטומטית מדי חמש שניות.")
    elif state.get("status") == "FAILED" or (
        state.get("status") in {"QUEUED", "RUNNING"} and not active
    ):
        st.error(state.get("message") or "ההרצה נקטעה וניתן לחדש אותה.")
    else:
        st.caption(state.get("message") or "הניסיון הסתיים.")
    st.caption(
        f"עדכון אחרון: {state.get('updated_at', '—')} · "
        f"זיכרון שיא: {float(state.get('peak_rss_mb', 0)):.1f}MB · "
        "ללא כתיבה לענן וללא קריאות מודל"
    )
    terminal_key = f"{state.get('run_id')}:{state.get('status')}"
    session_key = f"basket-terminal-seen-{case_id}"
    if not active and st.session_state.get(session_key) != terminal_key:
        st.session_state[session_key] = terminal_key
        st.rerun()


def score_label(score, na_reason=None):
    if score is None:
        return f"N/A — {na_reason}" if na_reason else "N/A"
    return str(score)


def rows_as_csv(rows):
    if not rows:
        return b""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def style_dashboard_figure(figure, *, height=None, legend_title=None):
    """Apply one quiet, high-contrast visual language to dashboard charts."""
    layout = {
        "paper_bgcolor": DASHBOARD_COLORS["plot"],
        "plot_bgcolor": DASHBOARD_COLORS["plot"],
        "font": {"color": DASHBOARD_COLORS["ink"], "size": 13},
        "legend": {
            "font": {"color": DASHBOARD_COLORS["ink"]},
            "title": {"font": {"color": DASHBOARD_COLORS["ink"]}},
        },
        "margin": {"l": 70, "r": 30, "t": 62, "b": 58},
        "hoverlabel": {
            "bgcolor": "#172033",
            "font": {"color": "#FFFFFF"},
            "bordercolor": "#172033",
        },
    }
    if height is not None:
        layout["height"] = height
    if legend_title is not None:
        layout["legend_title_text"] = legend_title
    figure.update_layout(**layout)
    figure.update_xaxes(
        gridcolor=DASHBOARD_COLORS["grid"],
        linecolor="#AEBAC8",
        tickfont={"color": DASHBOARD_COLORS["ink"]},
        title_font={"color": DASHBOARD_COLORS["ink"]},
        zeroline=False,
        automargin=True,
    )
    figure.update_yaxes(
        gridcolor=DASHBOARD_COLORS["grid"],
        linecolor="#AEBAC8",
        tickfont={"color": DASHBOARD_COLORS["ink"]},
        title_font={"color": DASHBOARD_COLORS["ink"]},
        zeroline=False,
        automargin=True,
    )
    figure.update_annotations(font={"color": DASHBOARD_COLORS["ink"]})
    return figure


def render_frame_summary(payload, *, key_prefix):
    distribution = payload["distribution"]
    st.markdown("### F · עוצמת המסגור")
    columns = st.columns(4)
    columns[0].metric("ממוצע F", f"{distribution['mean']:.1f}")
    columns[1].metric("חציון", f"{distribution['median']:.1f}")
    columns[2].metric(
        "טווח",
        f"{distribution['minimum']:.0f}–{distribution['maximum']:.0f}",
    )
    columns[3].metric(
        "הסכמה עד נקודה",
        f"{payload['agreement']['within_one_rate'] * 100:.1f}%",
    )
    language_labels = {"he": "עברית", "en": "אנגלית", "ar": "ערבית"}
    language_breakdown = payload["breakdowns"]["language"]
    frame_chart = go.Figure(
        go.Bar(
            x=[language_labels.get(code, code) for code in language_breakdown],
            y=[values["mean"] for values in language_breakdown.values()],
            text=[f"{values['mean']:.1f}" for values in language_breakdown.values()],
            textposition="outside",
            marker_color=DASHBOARD_COLORS["plum"],
            hovertemplate="%{x}<br>F=%{y:.1f}<extra></extra>",
        )
    )
    frame_chart.update_layout(
        title="עוצמת מסגור ממוצעת לפי שפת הסיקור",
        xaxis_title="שכבת שפה",
        yaxis_title="F · ציון 0–100",
        yaxis={"range": [0, 108]},
    )
    style_dashboard_figure(frame_chart, height=360)
    st.plotly_chart(
        frame_chart,
        width="stretch",
        config={"displaylogo": False},
        key=f"{key_prefix}-frame-language",
    )
    st.caption(
        "F הוא ציר תיאורי עצמאי: גבוה פירושו מסגור בולט ועקבי יותר. "
        "הוא אינו ציון אמת, איכות או הטיה, והוא מוצג לצד T ללא שקלול לציון יחיד."
    )




def render_aggregate_dashboard(case_bundles):
    """Render the active two-axis T/F comparison across closed cases."""
    document_rows = []
    case_records = []
    for case_id, short_label, records, frame_payload in case_bundles:
        case_record = next(item for item in records if item.level == "CASE")
        case_records.append((case_id, short_label, case_record, frame_payload))
        frame_by_document = {
            item["document_id"]: item["frame_score"]
            for item in frame_payload["records"]
        }
        for item in records:
            if item.level != "DOCUMENT":
                continue
            document_rows.append(
                {
                    "case_id": case_id,
                    "case_label": short_label,
                    "document_id": item.entity_id,
                    "title": item.label,
                    "source": item.details["source_name"],
                    "language": item.language,
                    "T": item.truth_score,
                    "F": frame_by_document[item.entity_id],
                }
            )
    frame = pd.DataFrame(document_rows)
    case_labels = [label for _, label, _, _ in case_records]

    st.success(
        f"{len(case_bundles)} קייסים סגורים וזמינים להשוואה "
        "בשני צירי הליבה: T ו־F."
    )
    st.markdown(
        f"""
        <section class="dashboard-metric-grid" aria-label="מדדי המאגר המתכלל">
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['blue']}">
            <div class="dashboard-metric-label">קייסים סגורים</div>
            <div class="dashboard-metric-value">{len(case_bundles)}</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['truth']}">
            <div class="dashboard-metric-label">כתבות עם T</div>
            <div class="dashboard-metric-value">{frame['T'].notna().sum()}/{len(frame)}</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['plum']}">
            <div class="dashboard-metric-label">כתבות עם F</div>
            <div class="dashboard-metric-value">{frame['F'].notna().sum()}/{len(frame)}</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['coverage']}">
            <div class="dashboard-metric-label">רכיבי ליבה</div>
            <div class="dashboard-metric-value">{8 * len(frame)}</div>
          </article>
        </section>
        <div class="dashboard-metric-note">
          <strong>המתודולוגיה הפעילה:</strong> T מודד נאמנות לאמת ו־F מודד
          את עוצמת המסגור. אלה שני צירים עצמאיים, ולכן אין מחברים אותם לציון
          יחיד. חריגות טקסטואליות קשות נשמרות כדגלי ביקורת בלבד ואינן ציר
          ניקוד נוסף.
        </div>
        """,
        unsafe_allow_html=True,
    )

    comparison = go.Figure()
    for label, values, color in (
        (
            "T · נאמנות לאמת",
            [item.truth_score for _, _, item, _ in case_records],
            DASHBOARD_COLORS["truth"],
        ),
        (
            "F · עוצמת מסגור",
            [payload["distribution"]["mean"] for _, _, _, payload in case_records],
            DASHBOARD_COLORS["plum"],
        ),
    ):
        comparison.add_trace(
            go.Bar(
                name=label,
                x=case_labels,
                y=values,
                text=[f"{value:.1f}" for value in values],
                textposition="outside",
                marker_color=color,
            )
        )
    comparison.update_layout(
        title="שני צירי הליבה בין הקייסים",
        barmode="group",
        yaxis={"range": [0, 108], "title": "ציון 0–100"},
    )
    style_dashboard_figure(comparison, height=450, legend_title="ציר")
    st.plotly_chart(
        comparison,
        width="stretch",
        config={"displaylogo": False},
        key="aggregate-case-comparison-v2",
    )

    st.markdown("### מפת המחקר · אמת מול מסגור")
    st.markdown(
        "<div class='dashboard-explainer'>כל נקודה היא כתבה. מיקום גבוה "
        "משקף נאמנות גבוהה יותר לאמת; מיקום ימני משקף מסגור חזק יותר. "
        "אין בתרשים הנחה שמסגור חזק הוא טוב או רע.</div>",
        unsafe_allow_html=True,
    )
    truth_frame = go.Figure()
    for case_id, short_label, _, _ in case_records:
        subset = frame[frame["case_id"] == case_id]
        truth_frame.add_trace(
            go.Scatter(
                x=subset["F"],
                y=subset["T"],
                mode="markers",
                name=short_label,
                marker={
                    "size": 11,
                    "color": CASE_COLORS[case_id],
                    "opacity": 0.78,
                    "line": {"color": "#FFFFFF", "width": 0.7},
                },
                customdata=subset[["source", "title"]].values,
                hovertemplate=(
                    "%{customdata[0]}<br>%{customdata[1]}<br>"
                    "F=%{x:.1f} · T=%{y:.1f}<extra></extra>"
                ),
            )
        )
    truth_frame.update_layout(
        xaxis_title="F · עוצמת מסגור",
        yaxis_title="T · נאמנות לאמת",
        xaxis={"range": [0, 102]},
        yaxis={"range": [0, 102]},
    )
    style_dashboard_figure(truth_frame, height=540, legend_title="קייס")
    st.plotly_chart(
        truth_frame,
        width="stretch",
        config={"displaylogo": False},
        key="aggregate-truth-frame-scatter-v2",
    )

def render_pending_case_dashboard(
    case_id, article_summary, frame_summary, review_payload
):
    """Render a truthful dashboard shell while final human decisions remain."""
    article_counts = article_summary.get("latest_status_counts") or {}
    article_complete = int(article_counts.get("SUCCESS") or 0)
    frame_counts = frame_summary.get("latest_status_counts") or {}
    frame_complete = int(frame_counts.get("SUCCESS") or 0)
    review_summary = review_payload.get("summary") or {}
    reviewed = int(review_summary.get("reviewed_dilemmas") or 0)
    auto_resolved = int(review_summary.get("auto_resolved") or 0)
    human_required = int(review_summary.get("human_required") or 0)

    st.warning(
        f"{case_id} ממתין ל־{human_required} פסיקות אנושיות. "
        "הדשבורד בנוי ומחובר לתוצרי הביניים, אך אינו מציג ציוני קייס "
        "סופיים לפני נעילת הפסיקות ו־T4."
    )
    st.markdown(
        f"""
        <section class="dashboard-metric-grid" aria-label="התקדמות {case_id}">
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['truth']}">
            <div class="dashboard-metric-label">כתבות שנותחו · T/N</div>
            <div class="dashboard-metric-value">{article_complete}/27</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['plum']}">
            <div class="dashboard-metric-label">כתבות שנותחו · F</div>
            <div class="dashboard-metric-value">{frame_complete}/27</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['blue']}">
            <div class="dashboard-metric-label">מחלוקות שנסגרו אוטומטית</div>
            <div class="dashboard-metric-value">{auto_resolved}/{reviewed}</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['coral']}">
            <div class="dashboard-metric-label">ממתינות להכרעת החוקר</div>
            <div class="dashboard-metric-value">{human_required}</div>
          </article>
        </section>
        <div class="dashboard-metric-note">
          <strong>זהו שלד מחקרי ולא תוצאה:</strong> כל 27 הכתבות כבר עברו
          ניתוח T/N ושתי קריאות F. לאחר הפסיקות ייבנו ציוני המסמכים,
          חתכי השפה והמקור, מסלולי הזמן, מפת T×F ותרשים T4 — ללא שינוי
          ידני של התשתית.
        </div>
        """,
        unsafe_allow_html=True,
    )

    agreement = frame_summary.get("agreement") or {}
    st.markdown("### בקרת איכות שכבר הושלמה")
    quality_a, quality_b, quality_c = st.columns(3)
    quality_a.metric(
        "הסכמת F מדויקת",
        f"{100 * float(agreement.get('exact_rate') or 0):.1f}%",
    )
    quality_b.metric(
        "הסכמה בטווח נקודה",
        f"{100 * float(agreement.get('within_one_rate') or 0):.1f}%",
    )
    quality_c.metric(
        "מסמכי F שהוסלמו",
        str(int(agreement.get("documents_escalated") or 0)),
    )
    st.info(
        "רכיבי התוצאה הסופיים ייפתחו אוטומטית כאשר ייווצרו "
        "final_results_v1.json ו־frame_intensity_results_v1.json."
    )


def render_closed_case_dashboard(
    case_id, score_records, frame_payload, *, human_decisions
):
    """Render one closed case using T and F as independent core axes."""
    key_prefix = case_id.lower().replace("-", "")
    case_record = next(item for item in score_records if item.level == "CASE")
    frame_records = frame_payload["records"]
    frame_by_document = {item["document_id"]: item for item in frame_records}
    frame_mean = frame_payload["distribution"]["mean"]

    st.success(
        f"{case_id} נסגר מחקרית: 27 כתבות, 216 רכיבי ליבה, "
        f"{human_decisions} הכרעות אנושיות אפקטיביות ותשע הכרעות מסלול "
        "נכללו בחישוב."
    )
    st.markdown(
        f"""
        <section class="dashboard-metric-grid" aria-label="מדדי {case_id}">
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['truth']}">
            <div class="dashboard-metric-label">T · נאמנות לאמת</div>
            <div class="dashboard-metric-value">{case_record.truth_score:.1f}</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['plum']}">
            <div class="dashboard-metric-label">F · עוצמת מסגור</div>
            <div class="dashboard-metric-value">{frame_mean:.1f}</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['coverage']}">
            <div class="dashboard-metric-label">כיסוי T</div>
            <div class="dashboard-metric-value">{100 * case_record.overall_coverage:.1f}%</div>
          </article>
          <article class="dashboard-metric-card" style="--metric-accent:{DASHBOARD_COLORS['blue']}">
            <div class="dashboard-metric-label">כיסוי F</div>
            <div class="dashboard-metric-value">{len(frame_records)}/27</div>
          </article>
        </section>
        <div class="dashboard-metric-note">
          <strong>פירוש החלוניות:</strong> T מודד נאמנות לעוגנים, למקורות
          ולאי־ודאות; F מודד את חוזק ועקביות המסגרת. שני הצירים מוצגים
          בנפרד ואינם מתחברים לציון מסכם. A יצא משימוש; N מופיע רק בנספח
          האבחוני.
        </div>
        """,
        unsafe_allow_html=True,
    )
    if case_record.critical_flags:
        st.markdown(
            "<div class='dashboard-flag'><strong>דגל עובדתי שאינו מתקזז "
            "בממוצע:</strong> נמצא לפחות עימות עובדתי קריטי באחת הכתבות. "
            "הדגל נשמר לצד ציר T.</div>",
            unsafe_allow_html=True,
        )

    render_frame_summary(frame_payload, key_prefix=f"{key_prefix}-v2")
    language_names = {"he": "עברית", "en": "אנגלית", "ar": "ערבית"}
    language_order = {"he": 0, "en": 1, "ar": 2}
    strata = sorted(
        [item for item in score_records if item.level == "STRATUM"],
        key=lambda item: language_order[item.language],
    )
    frame_language = frame_payload["breakdowns"]["language"]
    st.markdown("### חתך לפי שכבת שפה")
    language_chart = go.Figure()
    language_chart.add_trace(
        go.Bar(
            name="T · נאמנות לאמת",
            x=[language_names[item.language] for item in strata],
            y=[item.truth_score for item in strata],
            text=[f"{item.truth_score:.1f}" for item in strata],
            textposition="outside",
            marker_color=DASHBOARD_COLORS["truth"],
        )
    )
    language_chart.add_trace(
        go.Bar(
            name="F · עוצמת מסגור",
            x=[language_names[item.language] for item in strata],
            y=[frame_language[item.language]["mean"] for item in strata],
            text=[f"{frame_language[item.language]['mean']:.1f}" for item in strata],
            textposition="outside",
            marker_color=DASHBOARD_COLORS["plum"],
        )
    )
    language_chart.update_layout(
        title="שני צירי הליבה לפי שפת הסיקור",
        yaxis_title="ציון 0–100",
        barmode="group",
        yaxis={"range": [0, 108]},
    )
    style_dashboard_figure(language_chart, height=430, legend_title="ציר")
    st.plotly_chart(
        language_chart,
        width="stretch",
        config={"displaylogo": False},
        key=f"{key_prefix}-language-core-v2",
    )

    slots = [item for item in score_records if item.level == "PUBLISHER_SLOT"]
    frame_slot_means = (
        pd.DataFrame(
            [
                {
                    "publisher_slot": item["publisher_slot"],
                    "F": item["frame_score"],
                }
                for item in frame_records
            ]
        )
        .groupby("publisher_slot")["F"]
        .mean()
        .to_dict()
    )
    st.markdown("### תוצאות לפי משבצת מקור")
    st.dataframe(
        [
            {
                "שפה": language_names[item.language],
                "גוף תקשורת": item.label,
                "T": round(item.truth_score, 1),
                "F": round(frame_slot_means[item.entity_id], 1),
                "T4": score_label(
                    item.details["component_means"]["T4"],
                    item.details.get("t4_na_reason"),
                ),
                "כיסוי T": f"{100 * item.overall_coverage:.1f}%",
            }
            for item in sorted(
                slots,
                key=lambda row: (language_order[row.language], row.label),
            )
        ],
        hide_index=True,
        width="stretch",
    )

    documents = [item for item in score_records if item.level == "DOCUMENT"]
    phase_order = {
        "INITIAL_REPORT": 0,
        "EARLY_UPDATE": 1,
        "LATER_INVESTIGATION": 2,
    }
    phase_labels = [
        RESULT_PHASE_LABELS_HE[phase]
        for phase in ("INITIAL_REPORT", "EARLY_UPDATE", "LATER_INVESTIGATION")
    ]
    by_slot = {}
    for item in documents:
        by_slot.setdefault(item.details["publisher_slot"], []).append(item)
    slot_labels = {item.entity_id: item.label for item in slots}
    st.markdown("### שינוי לאורך שלבי הסיקור")
    st.markdown(
        "<div class='dashboard-explainer'>כל קו הוא משבצת מקור. התרשים "
        "העליון מציג T והתחתון F, מן הדיווח הראשוני ועד הבדיקה המאוחרת.</div>",
        unsafe_allow_html=True,
    )
    trajectory_tabs = st.tabs(
        [
            "תקשורת מקומית · עברית",
            "תקשורת בינלאומית · אנגלית",
            "תקשורת אזורית · ערבית",
        ]
    )
    for language, language_tab in zip(("he", "en", "ar"), trajectory_tabs):
        with language_tab:
            figure = make_subplots(
                rows=2,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.13,
                subplot_titles=("T · נאמנות לאמת", "F · עוצמת מסגור"),
            )
            language_slots = [
                (slot, rows)
                for slot, rows in sorted(by_slot.items())
                if rows[0].language == language
            ]
            for color_index, (slot, rows) in enumerate(language_slots):
                rows.sort(key=lambda row: phase_order[row.details["phase"]])
                color = SLOT_COLORS[color_index % len(SLOT_COLORS)]
                label = slot_labels[slot]
                for row_number, values, metric in (
                    (1, [row.truth_score for row in rows], "T"),
                    (
                        2,
                        [frame_by_document[row.entity_id]["frame_score"] for row in rows],
                        "F",
                    ),
                ):
                    figure.add_trace(
                        go.Scatter(
                            x=phase_labels,
                            y=values,
                            mode="lines+markers",
                            name=label,
                            line={"color": color, "width": 3},
                            marker={"size": 10, "color": color},
                            customdata=[row.label for row in rows],
                            hovertemplate=(
                                f"%{{customdata}}<br>{metric}=%{{y:.1f}}"
                                f"<extra>{label}</extra>"
                            ),
                            legendgroup=slot,
                            showlegend=row_number == 1,
                        ),
                        row=row_number,
                        col=1,
                    )
            figure.update_yaxes(range=[0, 100], dtick=20, row=1, col=1)
            figure.update_yaxes(range=[0, 100], dtick=20, row=2, col=1)
            figure.update_layout(hovermode="x unified")
            style_dashboard_figure(figure, height=620, legend_title="משבצת מקור")
            st.plotly_chart(
                figure,
                width="stretch",
                config={"displaylogo": False},
                key=f"{key_prefix}-trajectory-{language}-v2",
            )

    st.markdown("### מפת חום · רכיבי T ו־F")
    component_order = ["T1", "T2", "T3", "F1", "F2", "F3", "F4", "F5"]
    language_groups = (
        ("he", "תקשורת מקומית · עברית"),
        ("en", "תקשורת בינלאומית · אנגלית"),
        ("ar", "תקשורת אזורית · ערבית"),
    )
    for group_index, (language, group_title) in enumerate(language_groups):
        st.markdown(f"#### {group_title}")
        group_rows = sorted(
            [item for item in documents if item.language == language],
            key=lambda item: (
                item.details["publisher_slot"],
                phase_order[item.details["phase"]],
            ),
        )
        heat_values, heat_text, heat_labels = [], [], []
        for item in group_rows:
            truth_components = item.details["component_scores"]
            frame_components = frame_by_document[item.entity_id]["component_scores"]
            values = [
                truth_components.get(component)
                if component.startswith("T")
                else frame_components.get(component)
                for component in component_order
            ]
            heat_values.append(values)
            heat_text.append(
                [f"{component}: {score_label(value)}" for component, value in zip(component_order, values)]
            )
            heat_labels.append(
                f"{item.details['source_name']} · "
                f"{RESULT_PHASE_LABELS_HE[item.details['phase']]}"
            )
        heatmap = go.Figure(
            go.Heatmap(
                z=heat_values,
                x=component_order,
                y=heat_labels,
                text=heat_text,
                zmin=0,
                zmax=4,
                colorscale=HEATMAP_COLORSCALE,
                showscale=group_index == 0,
                colorbar={"title": "ציון<br>0–4"},
                hovertemplate="%{y}<br>%{text}<extra></extra>",
            )
        )
        style_dashboard_figure(heatmap, height=370)
        heatmap.update_layout(margin={"l": 190, "r": 80, "t": 18, "b": 48})
        st.plotly_chart(
            heatmap,
            width="stretch",
            config={"displaylogo": False},
            key=f"{key_prefix}-heatmap-{language}-v2",
        )

    st.markdown("### T4 · תיקון ועדכון לאורך זמן")
    t4_scored = [
        item for item in slots if item.details["component_means"]["T4"] is not None
    ]
    t4_scored.sort(key=lambda item: item.label)
    t4_chart = go.Figure(
        go.Bar(
            x=[item.details["component_means"]["T4"] for item in t4_scored],
            y=[item.label for item in t4_scored],
            orientation="h",
            text=[str(item.details["component_means"]["T4"]) for item in t4_scored],
            textposition="outside",
            marker_color=[
                {
                    0: "#7D3F4E",
                    1: DASHBOARD_COLORS["coral"],
                    2: "#8995A6",
                    3: DASHBOARD_COLORS["blue"],
                    4: DASHBOARD_COLORS["truth"],
                }[item.details["component_means"]["T4"]]
                for item in t4_scored
            ],
        )
    )
    t4_chart.update_layout(
        xaxis={"range": [0, 4.35], "dtick": 1}, xaxis_title="ציון 0–4"
    )
    style_dashboard_figure(t4_chart, height=430)
    st.plotly_chart(
        t4_chart,
        width="stretch",
        config={"displaylogo": False},
        key=f"{key_prefix}-t4-v2",
    )
    na_t4 = [item.label for item in slots if item.details["component_means"]["T4"] is None]
    if na_t4:
        st.caption("N/A בשל פיצול משבצת בין גופים: " + " · ".join(na_t4))

    st.markdown("### פיזור הכתבות על צירי T ו־F")
    tf_scatter = go.Figure(
        go.Scatter(
            x=[frame_by_document[item.entity_id]["frame_score"] for item in documents],
            y=[item.truth_score for item in documents],
            mode="markers",
            marker={
                "size": 11,
                "color": [CASE_COLORS[case_id]] * len(documents),
                "opacity": 0.78,
                "line": {"color": "#FFFFFF", "width": 0.7},
            },
            customdata=[
                [item.details["source_name"], item.label] for item in documents
            ],
            hovertemplate=(
                "%{customdata[0]}<br>%{customdata[1]}<br>"
                "F=%{x:.1f} · T=%{y:.1f}<extra></extra>"
            ),
        )
    )
    tf_scatter.update_layout(
        xaxis_title="F · עוצמת מסגור",
        yaxis_title="T · נאמנות לאמת",
        xaxis={"range": [0, 102]},
        yaxis={"range": [0, 102]},
    )
    style_dashboard_figure(tf_scatter, height=500)
    st.plotly_chart(
        tf_scatter,
        width="stretch",
        config={"displaylogo": False},
        key=f"{key_prefix}-tf-scatter",
    )

def show_anchor(anchor, review=None):
    st.markdown(f"**{anchor['anchor_id']}** — {anchor['canonical_proposition']}")
    if review:
        role = ANCHOR_ROLE_HE.get(review.get("role"), review.get("role", ""))
        st.markdown(f"**מעמד בתיק:** {role}")
        st.write(review.get("reason_he", ""))
    st.caption(anchor.get("scope_notes", ""))
    for assessment in anchor.get("assessments", []):
        when = assessment.get("as_of", {}).get("value", "")
        status = assessment.get("epistemic_status", "")
        confidence = assessment.get("confidence", "")
        st.write(f"נכון ל־{when}: `{status}` · ביטחון `{confidence}`")
        st.write(assessment.get("basis_summary", ""))


def show_visual_exhibit(exhibit):
    with st.container(border=True):
        st.markdown(f"##### {exhibit['title_he']}")
        if exhibit["media_type"] == "video":
            video_url = html.escape(exhibit["media_url"], quote=True)
            poster_url = html.escape(exhibit["poster_url"], quote=True)
            st.html(
                f"""
                <div dir="rtl" style="width:100%">
                  <video controls playsinline preload="metadata" poster="{poster_url}"
                         style="display:block;width:100%;height:auto;max-height:70vh;
                                background:#111;border-radius:8px">
                    <source src="{video_url}" type="video/mp4">
                    הדפדפן אינו תומך בנגן הווידאו.
                  </video>
                  <p style="margin:.45rem 0 0;color:#777;font-size:.9rem">
                    לחצו על ▶ להפעלה. זהו תיעוד לילה כהה; הפריים המוצג לפני
                    ההפעלה הוא תמונת הפתיחה שסיפק המפרסם.
                  </p>
                </div>
                """
            )
            st.link_button("פתיחת קובץ הווידאו ישירות", exhibit["media_url"])
            with st.expander("הצגת הפריים המייצג בנפרד"):
                st.image(exhibit["poster_url"], width="stretch")
        else:
            st.image(exhibit["poster_url"], width="stretch")
        st.markdown(f"**תיאור המוצג:** {exhibit['caption_he']}")
        st.caption(exhibit["context_he"])
        supports, limits = st.columns(2, gap="large")
        with supports:
            st.success(f"**מה המוצג מחזק**\n\n{exhibit['proves_he']}")
        with limits:
            st.warning(f"**מה המוצג אינו מוכיח**\n\n{exhibit['does_not_prove_he']}")
        with st.expander("כיתוב המקור ושרשרת המוצא"):
            st.markdown(
                f"<div class='evidence' dir='ltr'>{html.escape(exhibit['source_caption'])}</div>",
                unsafe_allow_html=True,
            )
            provenance = exhibit["provenance"]
            st.caption(
                f"מפרסם: {provenance['publisher']} · "
                f"חומר מקור: {provenance['underlying_footage']} · "
                f"מזהה מוצג: {exhibit['exhibit_id']}"
            )
            st.code(provenance["snapshot_hash"], language=None)
        st.link_button("פתיחת הכתבה שבה הוצג המוצג", exhibit["source_page_url"])



material = get_material(case_material_input_signature())
policy_registry = get_policy_registry(
    POLICY_PATH.stat().st_mtime_ns,
    POLICY_APPLICATION_PATH.stat().st_mtime_ns,
)
store = get_store()
store.backfill_policy_metadata(material.dilemmas)
current = store.current_decisions()
store.sync_score_ledger(material)
score_ledger = store.score_ledger_rows()
effective_rows = build_effective_rows(material, current)
t4_trajectories = build_t4_trajectories(BASE_DIR, material, effective_rows)
current_t4 = store.current_trajectory_decisions()
t4_readiness = build_scoring_readiness(
    material, effective_rows, t4_trajectories, current_t4
)
precedents = build_precedent_records(material, current, policy_registry)
precedent_conflicts = find_precedent_conflicts(precedents, policy_registry)
workspace_overview = build_workspace_overview(
    BASE_DIR,
    material,
    current,
    effective_rows=effective_rows,
    ledger_records=len(score_ledger),
    active_precedents=len(precedents),
    precedent_conflicts=len(precedent_conflicts),
)
queue_rows = build_deterministic_queue(material, current)
sync_status = get_sync_status(workspace_input_signature(BASE_DIR))

principal_actionable, decided_dilemmas = split_dilemmas_by_status(
    material.dilemmas, current
)
decided_count = len(decided_dilemmas)
deferred_count = sum(
    decision_status(current.get(d["dilemma_id"])) == "DEFERRED"
    for d in principal_actionable
)
pending_count = len(principal_actionable) - deferred_count
human_docket_size = decided_count + len(principal_actionable)
automatically_filtered_count = len(material.dilemmas) - human_docket_size

with st.sidebar:
    st.title("⚖️ מרכז המחקר")
    st.caption("CASE-001 · עותק עבודה מקומי")
    if sync_status["synchronized"]:
        st.success("מסונכרן ל־snapshot האחרון")
    else:
        st.warning("יש שינויים מקומיים שטרם פורסמו")
    st.progress(
        decided_count / human_docket_size if human_docket_size else 1.0,
        text=f"{decided_count} מתוך {human_docket_size} הוכרעו",
    )
    col_a, col_b = st.columns(2)
    col_a.metric("ממתינים", pending_count)
    col_b.metric("נדחו", deferred_count)
    if automatically_filtered_count:
        st.caption(
            f"{automatically_filtered_count} רכיבים סוננו מסף האדם: "
            "הפער בין הטיעונים קטן משתי נקודות."
        )
    st.divider()
    source_options = sorted({d["article"].get("source_name") or d["source_id"] for d in material.dilemmas})
    source_filter = st.multiselect("מקור", source_options, default=source_options)
    component_options = sorted({d["component"] for d in material.dilemmas})
    component_filter = st.multiselect(
        "מדד", component_options, default=component_options,
        format_func=lambda value: f"{value} · {COMPONENT_LABELS[value]}",
    )
    st.divider()
    st.caption("פלט המודל אינו משתנה. כל החלטה נשמרת כגרסה חדשה ביומן המקומי.")

st.title("ALETHEIA · מרכז המחקר וחדר השיפוט")
st.write(
    "ממשק מקומי־תחילה: העבודה נשמרת במחשב, והענן מתעדכן רק בפרסום מפורש "
    "ומאומת. פתיחת המסך אינה מפעילה Vertex ואינה פונה ל־BigQuery."
)
if decided_count == 0:
    st.markdown(
        '<div class="pending-note">כל 11 ההכרעות נמצאות בהמתנה. שום המלצה קודמת לא הוזנה כפסק דין.</div>',
        unsafe_allow_html=True,
    )

overview_tab, generator_tab, queue_tab, court_tab, case2_court_tab, t4_tab, dashboards_tab, policy_tab, scores_tab, effective_tab, journal_tab = st.tabs(
    [
        "תמונת מצב",
        "מחולל הקייסים",
        "תור העבודה",
        "אולם הדיונים",
        "קייסים · שיפוט",
        "T4 · מסלולי עדכון",
        "דשבורדים",
        "ספר התקדימים",
        "טבלת הניקוד והנימוקים",
        "תוצאות אפקטיביות",
        "יומן החלטות",
    ]
)

with overview_tab:
    st.subheader("CASE-001 · מצב הפיילוט")
    metric_a, metric_b, metric_c, metric_d = st.columns(4)
    metric_a.metric("כתבות בקורפוס", workspace_overview.corpus_documents)
    metric_b.metric("נותחו", workspace_overview.analyzed_documents)
    metric_c.metric("טרם נותחו", workspace_overview.not_analyzed_documents)
    metric_d.metric("ממתינות לשיפוט", workspace_overview.documents_needing_review)

    result_a, result_b, result_c, result_d = st.columns(4)
    result_a.metric("רכיבים אפקטיביים", workspace_overview.effective_components)
    result_b.metric("פסקי דין נוכחיים", workspace_overview.current_decisions)
    result_c.metric("רשומות ביומן", workspace_overview.ledger_records)
    result_d.metric("תקדימים פעילים", workspace_overview.active_precedents)

    st.markdown("### כיסוי לפי שפה ושלב זמן")
    st.dataframe(
        [
            {
                "שפה": row["language_he"],
                "שלב": row["phase_he"],
                "הושלמו": row["reviewed"],
                "דורשות שיפוט": row["needs_review"],
                "טרם נותחו": row["not_analyzed"],
                "סך הכול": row["total"],
            }
            for row in workspace_overview.matrix
        ],
        hide_index=True,
        width="stretch",
    )

    st.markdown("### 27 הכתבות")
    overview_language = st.multiselect(
        "סינון שפה בתמונת המצב",
        options=["he", "en", "ar"],
        default=["he", "en", "ar"],
        format_func=lambda value: LANGUAGE_LABELS_HE[value],
    )
    overview_status = st.multiselect(
        "סינון מצב",
        options=["REVIEWED", "NEEDS_REVIEW", "NOT_ANALYZED"],
        default=["REVIEWED", "NEEDS_REVIEW", "NOT_ANALYZED"],
        format_func=lambda value: DOCUMENT_STATUS_HE[value],
    )
    st.dataframe(
        [
            {
                "מקור": row["source_name"],
                "שפה": row["language_he"],
                "שלב": row["phase_he"],
                "תאריך": row["published_at"],
                "כותרת": row["title"],
                "מצב": row["research_status_he"],
            }
            for row in workspace_overview.documents
            if row["language"] in overview_language
            and row["research_status"] in overview_status
        ],
        hide_index=True,
        width="stretch",
    )

    st.markdown("### מצב העותק והענן")
    sync_a, sync_b, sync_c = st.columns(3)
    sync_a.metric(
        "מצב",
        "מסונכרן" if sync_status["synchronized"] else "שינויים מקומיים",
    )
    sync_b.metric("פרסום אחרון", sync_status["published_at"] or "לא ידוע")
    sync_c.metric("זיכרון שיא של הממשק", f"{current_peak_rss_mb():.1f} MB")
    st.caption(
        "מזהה working copy: " + sync_status["local_fingerprint"][:16]
        + " · מקור מצב קנוני: " + sync_status["canonical_source"]
    )
    if sync_status["table_differences"]:
        with st.expander("פירוט השינויים המקומיים"):
            st.write(" · ".join(sync_status["table_differences"]))

    refresh_col, publish_col = st.columns(2, gap="large")
    with refresh_col:
        st.markdown("#### אימות/רענון מן הענן")
        st.caption(
            "פעולה לקריאה בלבד. אם הענן והעותק המקומי שונים, המערכת תעצור "
            "ולא תדרוס הכרעות."
        )
        if st.button("אימות snapshot הענן", key="verify-cloud"):
            try:
                with st.spinner("מוריד ומאמת hashes מן הענן…"):
                    run_sync_action("--verify-cloud")
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                st.error(f"האימות נעצר: {error}")
            else:
                st.success("הענן והעותק המקומי זהים. לא שונה מידע מחקרי.")
                st.rerun()
    with publish_col:
        st.markdown("#### פרסום snapshot")
        st.caption(
            "הפרסום יוצר גיבוי, מעדכן רק את שמונה טבלאות CASE-001 ומאמת "
            "את התוצאה בקריאה חוזרת."
        )
        confirm_publish = st.checkbox(
            "אני מאשר/ת לפרסם את עותק העבודה הנוכחי",
            key="confirm-publish",
        )
        if st.button(
            "פרסום snapshot מאומת",
            key="publish-cloud",
            type="primary",
            disabled=not confirm_publish or sync_status["synchronized"],
        ):
            try:
                with st.spinner("מגבה, מפרסם ומאמת…"):
                    run_sync_action("--publish")
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                st.error(f"הפרסום נעצר ללא מחיקת העותק המקומי: {error}")
            else:
                st.success("ה־snapshot פורסם ואומת במלואו.")
                st.rerun()

    if current_peak_rss_mb() > 300:
        st.error(
            "תהליך הממשק עבר 300 MB RSS. שער M4 נעצר עד בדיקת זיכרון."
        )

with generator_tab:
    st.subheader("מחולל הקייסים · שני שערי אישור")
    generator_notice = st.session_state.pop("case-generator-notice", None)
    if generator_notice:
        if generator_notice["level"] == "success":
            st.success(generator_notice["message"])
        else:
            st.warning(generator_notice["message"])
    st.info(
        "כאן מאשרים רק את נושא הקייס. אישור אינו מפעיל ניתוח ואינו שולח "
        "חומר לענן; הוא מאפשר למערכת להתחיל גילוי מקומי של סל הכתבות. "
        "הסל יובא לאישור נפרד לפני תחילת המחקר."
    )
    st.caption(
        "המועמדים מבוססים על רשומות גילוי ציבוריות שנאספו. Vertex רשאי "
        "לסכם ולסווג את הרשומות, אך אינו רשאי להמציא אירוע או לחשב ציוני מחקר. "
        "לאחר אישור הסל, T ו־F הם צירי הליבה; N הוא אינדיקציית קצה ו־A יצא משימוש."
    )

    case_registry = get_case_registry()
    all_candidates = case_registry.list_candidates()
    pending_candidates = [
        candidate
        for candidate in all_candidates
        if candidate["registry_state"] in {"PROPOSED", "AWAITING_TOPIC_APPROVAL"}
    ]
    approved_candidates = [
        candidate
        for candidate in all_candidates
        if candidate["registry_state"] == "APPROVED"
    ]
    rejected_candidates = [
        candidate
        for candidate in all_candidates
        if candidate["registry_state"] == "REJECTED"
    ]
    registered_cases = case_registry.list_cases()

    generator_a, generator_b, generator_c, generator_d = st.columns(4)
    generator_a.metric("קייסים במדגם", f"{1 + len(registered_cases)}/8")
    generator_b.metric("ממתינים לאישור נושא", len(pending_candidates))
    generator_c.metric("נושאים שאושרו בשער 1", len(approved_candidates))
    generator_d.metric("נדחו ותועדו", len(rejected_candidates))

    proposal_tab, basket_gate_tab, approved_tab, rejected_tab, selection_log_tab = st.tabs(
        [
            "ממתינים לטיפול",
            "אחרי שער 1",
            "נושאים שאושרו",
            "נדחו",
            "יומן בחירה",
        ]
    )

    scope_labels = {
        "CORE": "אירוע ליבה",
        "DIRECT_SPILLOVER": "השלכה ישירה",
        "EXTERNAL_INDIRECT": "אירוע חיצוני בעל השפעה ישירה",
        "PREWAR_FORMATIVE": "אירוע מכונן שקדם למלחמה",
    }
    responsibility_labels = {
        "ISRAEL": "ייחוס ראשוני לישראל",
        "NON_ISRAEL_ACTOR": "ייחוס ראשוני לשחקן שאינו ישראל",
        "SHARED_OR_CHAINED": "אחריות משותפת או שרשרתית",
        "UNRESOLVED": "ייחוס אחריות זמני: טרם מוכרע",
        "PROVISIONAL": "זמני — טעון מחקר",
    }
    clarity_labels = {
        "HIGH": "בהירות ראייתית גבוהה",
        "PARTIAL": "בהירות חלקית",
        "UNRESOLVED": "נותרו לפחות שתי חלופות סבירות",
    }

    with proposal_tab:
        if not pending_candidates:
            st.success("אין כרגע מועמד שממתין להכרעת נושא.")
            st.write(
                "מנוע הגילוי יוסיף לכאן מועמדים רק לאחר שיש להם עקבות ממספר "
                "מקורות ציבוריים והתאמה לחוזה המדגם."
            )
        else:
            selected_candidate_id = st.selectbox(
                "בחירת מועמד לבדיקה",
                options=[candidate["candidate_id"] for candidate in pending_candidates],
                format_func=lambda candidate_id: next(
                    candidate["title"]
                    for candidate in pending_candidates
                    if candidate["candidate_id"] == candidate_id
                ),
                key="case-generator-pending-candidate",
            )
            candidate = next(
                item
                for item in pending_candidates
                if item["candidate_id"] == selected_candidate_id
            )
            st.markdown(f"### {candidate['title']}")
            st.write(candidate["neutral_summary"])
            st.caption(
                f"חלון: {candidate['window_start']}–{candidate['window_end']} · "
                f"גרסה {candidate['version']} · hash {candidate['candidate_hash'][:12]}"
            )

            classification_a, classification_b, classification_c = st.columns(3)
            classification_a.metric(
                "זיקה למלחמה",
                scope_labels.get(candidate["scope_relation"], candidate["scope_relation"]),
            )
            classification_b.metric(
                "ייחוס לאיזון המדגם",
                responsibility_labels.get(
                    candidate["responsibility_class"],
                    candidate["responsibility_class"],
                ),
            )
            classification_c.metric(
                "בהירות ראשונית",
                clarity_labels.get(
                    candidate["evidence_clarity"], candidate["evidence_clarity"]
                ),
            )
            st.warning(
                "ייחוס האחריות והבהירות משמשים לאיזון המדגם בלבד. הם אינם "
                "ממצא עובדתי ואינם מנבאים את ציוני כלי התקשורת."
            )
            if candidate["evidence_clarity"] == "UNRESOLVED":
                hypotheses = candidate.get("unresolved_hypotheses") or []
                st.markdown("#### מה עדיין אינו מוכרע")
                st.write(
                    "הסיווג מתייחס לעובדה המרכזית שאינה ניתנת עדיין להכרעה "
                    "בביטחון גבוה — לא לעצם קיומה של מחלוקת ציבורית."
                )
                for hypothesis in hypotheses:
                    st.markdown(f"- {hypothesis}")
            st.markdown("#### מדוע האירוע נמצא בתוך עולם המחקר")
            st.write(candidate["war_nexus_rationale"])
            if candidate.get("turning_point_rationale"):
                st.markdown("#### מדוע זהו אירוע תפניתי")
                st.write(candidate["turning_point_rationale"])
                dimension_labels = {
                    "MILITARY_PHASE": "שינוי שלב צבאי",
                    "ACTOR_LEADERSHIP": "שינוי בהנהגת שחקן",
                    "DIPLOMATIC_ORDER": "שינוי בסדר הדיפלומטי",
                    "LEGAL_ORDER": "שינוי בסדר המשפטי",
                    "HUMANITARIAN_ORDER": "שינוי בסדר ההומניטרי",
                    "HOSTAGE_OR_CEASEFIRE_REGIME": "שינוי במשטר החטופים או הפסקת האש",
                    "REGIONAL_ESCALATION": "הסלמה אזורית",
                }
                st.caption(
                    "ממדי השינוי: "
                    + " · ".join(
                        dimension_labels.get(value, value)
                        for value in candidate.get("transformative_dimensions") or []
                    )
                )
            selection_evidence = candidate.get("selection_evidence") or {}
            peak = selection_evidence.get("peak") or {}
            if peak:
                st.markdown("#### עקבת הבאזז שהובילה למועמד")
                buzz_a, buzz_b, buzz_c = st.columns(3)
                buzz_a.metric("פריטים ביום השיא", f"{int(peak['article_count']):,}")
                buzz_b.metric(
                    "פי הבייסליין המקומי",
                    f"{float(peak['lift_over_local_baseline']):.1f}×",
                )
                buzz_c.metric(
                    "אחוזון בחלון",
                    f"{float(peak['window_percentile']) * 100:.0f}",
                )
                st.caption(
                    "הבאזז משמש לאיתור סדר־יום בלבד ואינו ציון מחקרי. "
                    "אחריות, אמת ותוצאת כלי התקשורת לא השתתפו בבחירת השיא."
                )
            elif selection_evidence.get("protocol") == "authoritative-turning-point-crosscheck-v1":
                st.markdown("#### מסלול גילוי מוסדי חלופי")
                st.write(
                    "המועמד אינו נשען על נתון נפח זמין. במקום זאת הוא נדרש "
                    "למקור ראשוני או מוסדי, להצלבה חדשותית עצמאית ולתוצאה "
                    "ששינתה מצב בפועל."
                )
                if selection_evidence.get("note_he"):
                    st.write(selection_evidence["note_he"])
                st.caption(
                    "היעדר מדד באזז אינו ראיה לחשיבות ואינו מוריד את רף "
                    "הקנוניות; סיבת המסלול והמקורות נשמרים לביקורת."
                )
            st.markdown("#### השפעה על איזון שמונת הקייסים")
            st.write(candidate["sample_balance_impact"]["rationale"])
            st.caption(
                "התחזית מחושבת לכל מועמד בנפרד מול הקייסים שכבר אושרו. "
                "מועמדים אחרים שממתינים בשער 1 אינם נספרים כאילו אושרו."
            )

            coverage_he, coverage_en, coverage_ar = st.columns(3)
            coverage_he.metric("רשומות גילוי בעברית", candidate["coverage_estimate"]["he"])
            coverage_en.metric("רשומות גילוי באנגלית", candidate["coverage_estimate"]["en"])
            coverage_ar.metric("רשומות גילוי בערבית", candidate["coverage_estimate"]["ar"])

            with st.expander("עקבות המקור שעליהם מבוססת ההצעה", expanded=True):
                st.dataframe(
                    [
                        {
                            "שפה": LANGUAGE_LABELS_HE[lead["language"]],
                            "מקור": lead["source_name"],
                            "תאריך": lead["published_at"],
                            "כותרת": lead["title"],
                            "קישור": lead["url"],
                        }
                        for lead in candidate["evidence_leads"]
                    ],
                    hide_index=True,
                    width="stretch",
                    column_config={"קישור": st.column_config.LinkColumn("קישור")},
                )
            with st.expander("מילות החיפוש המוצעות לסל הכתבות"):
                for language in ("he", "en", "ar"):
                    st.markdown(
                        f"**{LANGUAGE_LABELS_HE[language]}:** "
                        + " · ".join(candidate["proposed_keywords"][language])
                    )

            researcher_reason = st.text_area(
                "נימוק להחלטה",
                key=f"candidate-reason-{candidate['candidate_hash']}",
                placeholder="מדוע הקייס מוסיף או אינו מוסיף למחקר ולמנעד המדגם?",
            )
            approve_col, reject_col = st.columns(2, gap="large")
            if approve_col.button(
                "אישור נושא והתקדמות לגילוי סל",
                type="primary",
                key=f"approve-candidate-{candidate['candidate_hash']}",
            ):
                try:
                    case_id = approve_topic_candidate(
                        case_registry,
                        candidate,
                        actor="principal-researcher",
                        rationale=researcher_reason,
                    )
                except (CaseRegistryError, ValueError) as error:
                    st.error(f"האישור לא נשמר: {error}")
                else:
                    try:
                        start_basket_discovery_action(case_id)
                    except RuntimeError as error:
                        st.session_state["case-generator-notice"] = {
                            "level": "warning",
                            "message": (
                                f"נושא {case_id} אושר בשער 1, אך לא ניתן היה "
                                f"להתחיל את בניית הסל: {error}."
                            ),
                        }
                    else:
                        st.session_state["case-generator-notice"] = {
                            "level": "success",
                            "message": (
                                f"נושא {case_id} אושר בשער 1. בניית הסל התחילה "
                                "ברקע ותופיע בטאב 'אחרי שער 1'."
                            ),
                        }
                    st.rerun()
            if reject_col.button(
                "דחיית המועמד",
                key=f"reject-candidate-{candidate['candidate_hash']}",
                disabled=not researcher_reason.strip(),
            ):
                try:
                    reject_topic_candidate(
                        case_registry,
                        candidate,
                        actor="principal-researcher",
                        rationale=researcher_reason,
                    )
                except (CaseRegistryError, ValueError) as error:
                    st.error(f"הדחייה לא נשמרה: {error}")
                else:
                    st.success("המועמד נדחה ונשמר ביומן הבחירה בלי לצרוך מזהה CASE.")
                    st.rerun()

    with basket_gate_tab:
        waiting_baskets = [
            item
            for item in registered_cases
            if item["current_state"] == "AWAITING_BASKET_APPROVAL"
        ]
        discovery_cases = [
            item
            for item in registered_cases
            if item["current_state"] == "BASKET_DISCOVERY"
        ]

        st.markdown("### עברו שער 1 · הסל בבנייה")
        st.caption(
            "כאן מופיעים כל הנושאים שאושרו בשער 1 אך עדיין לא הגיעו לאישור "
            "האנושי של סל הכתבות. כשל ספק מוצג בנפרד ממחסור אמיתי בכתבות."
        )
        if not discovery_cases:
            st.info("אין כרגע קייס בתהליך בניית סל.")
        else:
            discovery_rows = []
            discovery_reports = {}
            report_status_he = {
                "DISCOVERY_FAILED": "ספקי האיתור לא היו זמינים",
                "INSUFFICIENT_BASKET": "הסל עדיין חסר",
                "HUMAN_DECISION_REQUIRED": "נדרשת הכרעת מקור חלופי",
                "READY_FOR_GATE": "הסל הושלם",
            }
            for item in discovery_cases:
                report = load_basket_discovery_report(item["case_id"])
                run_state = load_run_state(BASE_DIR, item["case_id"])
                discovery_reports[item["case_id"]] = report
                state_label = {
                    "QUEUED": "בתור להרצה",
                    "RUNNING": "בבנייה כעת",
                    "WAITING_RETRY": "ממתין להשלמה",
                    "AWAITING_HUMAN_DECISION": "נדרשת הכרעת מקור חלופי",
                    "FAILED": "נעצר — ניתן לחדש",
                }.get((run_state or {}).get("status"))
                discovery_rows.append(
                    {
                        "קייס": item["case_id"],
                        "נושא": item["title"],
                        "מצב הניסיון האחרון": (
                            state_label
                            or report_status_he.get(report.get("status"), report.get("status"))
                            if report
                            else state_label or "טרם בוצע ניסיון"
                        ),
                        "קישורים שנמצאו": report.get("leads_discovered", 0) if report else 0,
                        "כתבות שעברו בדיקה": int(
                            (run_state or {}).get(
                                "articles_staged",
                                report.get("articles_staged", 0) if report else 0,
                            )
                        ),
                        "משבצות חסרות": int(
                            (run_state or {}).get(
                                "missing_cells",
                                len(report.get("missing_cells", [])) if report else 27,
                            )
                        ),
                        "ניסיון אחרון": (
                            (run_state or {}).get("updated_at")
                            or (report or {}).get("generated_at", "—")
                        ),
                    }
                )
            st.dataframe(discovery_rows, hide_index=True, width="stretch")
            retry_case_id = st.selectbox(
                "בחירת קייס לבדיקת מצב או להפעלה חוזרת",
                options=[item["case_id"] for item in discovery_cases],
                format_func=lambda case_id: next(
                    f"{item['case_id']} · {item['title']}"
                    for item in discovery_cases
                    if item["case_id"] == case_id
                ),
                key="case-generator-retry-discovery",
            )
            selected_report = discovery_reports.get(retry_case_id)
            selected_run_state = load_run_state(BASE_DIR, retry_case_id)
            render_basket_run_status(retry_case_id)
            if selected_report:
                if selected_report.get("phase_policy_version"):
                    st.caption(
                        "חלונות הסיקור בדוח זה: יום האירוע והיום שאחריו = "
                        "דיווח ראשוני; שני הימים הבאים = עדכון מוקדם; "
                        "מהיום החמישי ואילך = בדיקה מאוחרת."
                    )
                if selected_report.get("status") == "DISCOVERY_FAILED":
                    st.warning(
                        "הניסיון האחרון נעצר מפני שספקי האיתור הציבוריים לא "
                        "היו זמינים. אפס הכתבות אינו מוצג עוד כאילו אפס כתבות פורסמו."
                    )
                elif selected_report.get("status") == "INSUFFICIENT_BASKET":
                    st.warning(
                        f"עברו בדיקה {selected_report.get('articles_staged', 0)} כתבות; "
                        "טרם הושג סף 25. המשבצות החסרות מתועדות בדוח."
                    )
                elif selected_report.get("status") == "HUMAN_DECISION_REQUIRED":
                    st.error(
                        "הגילוי והניסיונות החוזרים מוצו, ועדיין אין 25 כתבות. "
                        "לא נוריד את הרף ולא נערבב גופים אוטומטית: נדרשת "
                        "הכרעתך על גוף חלופי בעל פרופיל ערכי דומה."
                    )
                elif selected_report.get("status") == "NO_PROGRESS":
                    st.warning(
                        "סבב ההשלמה הסתיים בלי כתבה חדשה או החלפה מאומתת. "
                        "הקייס נשאר בבניית הסל ולא הוחזר לשער 2."
                    )
                provider_counts = selected_report.get("providers") or {}
                if provider_counts:
                    st.caption(
                        "תוצאות לפי ספק: "
                        + " · ".join(
                            f"{provider}: {count}"
                            for provider, count in provider_counts.items()
                        )
                    )
                failure_counts = {
                    "חסימת אתר": 0,
                    "תוכן לא תואם": 0,
                    "ארכיון/רשת לא זמינים": 0,
                    "כפילות": 0,
                    "אחר": 0,
                }
                for failure in selected_report.get("failures") or []:
                    message = str(failure.get("message") or "").lower()
                    if "http 403" in message or "http 429" in message:
                        failure_counts["חסימת אתר"] += 1
                    elif "configured case keyword" in message or "too short" in message:
                        failure_counts["תוכן לא תואם"] += 1
                    elif "duplicate" in message:
                        failure_counts["כפילות"] += 1
                    elif "timed out" in message or "http 503" in message:
                        failure_counts["ארכיון/רשת לא זמינים"] += 1
                    else:
                        failure_counts["אחר"] += 1
                visible_failures = {
                    label: count for label, count in failure_counts.items() if count
                }
                if visible_failures:
                    st.caption("סיבות לאי־קליטה בניסיון האחרון")
                    failure_columns = st.columns(len(visible_failures))
                    for column, (label, count) in zip(
                        failure_columns, visible_failures.items()
                    ):
                        column.metric(label, count)
                uncovered_cells = (
                    selected_report.get("uncovered_by_discovery") or []
                )
                if uncovered_cells:
                    st.error(
                        f"ל־{len(uncovered_cells)} משבצות לא נמצא אפילו קישור מועמד "
                        "תחת הגדרת שלבי הסיקור הנוכחית. זהו חסם דגימה, לא כשל HTML."
                    )
                missing_cells = selected_report.get("missing_cells") or []
                if missing_cells:
                    with st.expander(
                        f"המשבצות שעדיין חסרות ({len(missing_cells)})",
                        expanded=True,
                    ):
                        st.dataframe(
                            [
                                {
                                    "משבצת מפרסם": cell["publisher_slot"],
                                    "שלב סיקור": PHASE_HE.get(
                                        cell["sample_phase"], cell["sample_phase"]
                                    ),
                                }
                                for cell in missing_cells
                            ],
                            hide_index=True,
                            width="stretch",
                        )
                gap_details = selected_report.get("gap_details") or []
                if selected_report.get("requires_human_decision") and gap_details:
                    st.markdown("#### ההכרעה שנדרשת ממך")
                    st.dataframe(
                        [
                            {
                                "משבצת מפרסם": gap["publisher_slot"],
                                "שלב סיקור": PHASE_HE.get(
                                    gap["sample_phase"], gap["sample_phase"]
                                ),
                                "מצב": (
                                    "לא נמצא קישור מועמד"
                                    if gap["block_reason"] == "NO_CANDIDATE_DISCOVERED"
                                    else "הקישורים שנמצאו לא עברו בדיקת HTML"
                                ),
                            }
                            for gap in gap_details
                        ],
                        hide_index=True,
                        width="stretch",
                    )
                    st.caption(
                        "לאחר בחירת גוף חלופי אחד לכל משבצת, המערכת תחפש בו "
                        "שלוש כתבות ותמנע שעטנז של שלושה גופים."
                    )
                staged_articles = selected_report.get("staged_articles") or []
                if staged_articles:
                    with st.expander(
                        f"הכתבות שכבר עברו בדיקה ({len(staged_articles)})"
                    ):
                        st.dataframe(
                            [
                                {
                                    "שפה": LANGUAGE_LABELS_HE.get(
                                        article["language"], article["language"]
                                    ),
                                    "גוף": article["source_name"],
                                    "שלב": PHASE_HE.get(
                                        article["sample_phase"],
                                        article["sample_phase"],
                                    ),
                                    "תאריך": article["published_at"],
                                    "כותרת": article["title"],
                                    "קישור": article["url"],
                                }
                                for article in staged_articles
                            ],
                            hide_index=True,
                            width="stretch",
                            column_config={
                                "קישור": st.column_config.LinkColumn("קישור")
                            },
                        )
            if st.button(
                "המשך בניית הסל מהמשבצות החסרות",
                type="primary",
                key=f"retry-basket-discovery-{retry_case_id}",
                disabled=(
                    run_is_active(selected_run_state)
                    or bool(
                        (selected_report or {}).get("requires_human_decision")
                    )
                ),
            ):
                try:
                    start_basket_discovery_action(retry_case_id)
                except RuntimeError as error:
                    st.error(f"לא ניתן היה להתחיל את ההשלמה: {error}")
                else:
                    st.success(
                        "ההשלמה התחילה ברקע. הסטטוס יתעדכן כאן בלי לחסום את הממשק."
                    )
                    st.rerun()

        st.divider()
        st.markdown("### ממתינים לאישורך בשער 2")
        st.caption(
            "רק הסלים שהגיעו לסף המתודולוגי מופיעים כאן. אישור שלהם מקפיא "
            "את גרסת הסל ומאפשר להפעיל את פייפליין המחקר."
        )
        if not waiting_baskets:
            st.info("אין כרגע סל כתבות שהושלם וממתין לאישורך.")
        else:
            selected_basket_case = st.selectbox(
                "בחירת סל לבדיקה",
                options=[item["case_id"] for item in waiting_baskets],
                format_func=lambda case_id: next(
                    f"{item['case_id']} · {item['title']}"
                    for item in waiting_baskets
                    if item["case_id"] == case_id
                ),
                key="case-generator-basket-case",
            )
            basket = case_registry.get_basket(selected_basket_case)
            basket_articles = basket["articles"]
            basket_a, basket_b, basket_c, basket_d = st.columns(4)
            basket_a.metric("כתבות", len(basket_articles))
            basket_b.metric(
                "עברית / אנגלית / ערבית",
                " / ".join(
                    str(sum(article["language"] == language for article in basket_articles))
                    for language in ("he", "en", "ar")
                ),
            )
            basket_c.metric(
                "משבצות עם החלפה",
                len({
                    article["publisher_slot"]
                    for article in basket_articles
                    if article["requires_human_approval"]
                }),
            )
            basket_d.metric("גרסת סל", basket["version"])
            st.caption(
                f"hash לאישור: {basket['basket_hash']} · כל שינוי בחברות הסל "
                "ייצור hash חדש ויבטל את האישור לגרסה זו בלבד."
            )
            st.dataframe(
                [
                    {
                        "שפה": LANGUAGE_LABELS_HE[article["language"]],
                        "משבצת": article["publisher_slot"],
                        "גוף": article["source_name"],
                        "שלב": PHASE_HE[article["sample_phase"]],
                        "תאריך": article["published_at"],
                        "כותרת": article["title"],
                        "HTML": "נשמר מקומית" if article["snapshot_ref"] else "חסר",
                        "חריגת החלפה": (
                            "דורשת אישור" if article["requires_human_approval"] else "—"
                        ),
                        "קישור": article["url"],
                    }
                    for article in basket_articles
                ],
                hide_index=True,
                width="stretch",
                column_config={"קישור": st.column_config.LinkColumn("קישור")},
            )
            replacement_slots = sorted({
                article["publisher_slot"]
                for article in basket_articles
                if article["requires_human_approval"]
            })
            if replacement_slots:
                st.markdown("#### ההכרעה המתודולוגית בשער 2")
                for slot_id in replacement_slots:
                    slot_articles = [
                        article
                        for article in basket_articles
                        if article["publisher_slot"] == slot_id
                    ]
                    st.warning(
                        replacement_slot_decision_copy(
                            BASKET_SLOT_HE.get(slot_id, slot_id), slot_articles
                        )
                    )
                    st.dataframe(
                        [
                            {
                                "גוף": article["source_name"],
                                "שלב": PHASE_HE[article["sample_phase"]],
                                "כותרת": article["title"],
                                "קישור": article["url"],
                            }
                            for article in slot_articles
                        ],
                        hide_index=True,
                        width="stretch",
                        column_config={"קישור": st.column_config.LinkColumn("קישור")},
                    )
            discovery_report = load_basket_discovery_report(selected_basket_case)
            missing_cells = (discovery_report or {}).get("missing_cells", [])
            if missing_cells:
                missing_text = " · ".join(
                    f"{BASKET_SLOT_HE.get(cell['publisher_slot'], cell['publisher_slot'])} — "
                    f"{PHASE_HE.get(cell['sample_phase'], cell['sample_phase'])}"
                    for cell in missing_cells
                )
                st.info(
                    f"הסל עומד ברף 25/27. שתי המשבצות שלא נכללו: {missing_text}. "
                    "אישור הסל מקבל גם את החסר המתועד הזה."
                )
            selected_case = case_registry.get_case(selected_basket_case)
            methodology = str(
                (selected_case.get("manifest") or {}).get("methodology_version") or ""
            )
            topic_audit = load_topic_relevance_audit(selected_basket_case)
            audit_allows_approval = methodology != "case-generator-contract-v1.2"
            audit_matches_basket = bool(
                topic_audit
                and topic_audit.get("basket_hash") == basket.get("basket_hash")
            )
            if methodology == "case-generator-contract-v1.2":
                audit_allows_approval = bool(
                    audit_matches_basket
                    and topic_audit.get("status") == "PASS"
                )
            if methodology == "case-generator-contract-v1.2" or audit_matches_basket:
                st.markdown("#### בדיקת התאמת הנושא")
                if not audit_matches_basket:
                    st.error("טרם הושלמה בדיקת התאמת נושא לסל המדויק הזה.")
                else:
                    audit_summary = topic_audit.get("summary") or {}
                    audit_cols = st.columns(3)
                    audit_cols[0].metric("עברו", audit_summary.get("PASS", 0))
                    audit_cols[1].metric(
                        "דורשות החלפה", audit_summary.get("REPAIR_REQUIRED", 0)
                    )
                    audit_cols[2].metric(
                        "דורשות הכרעה", audit_summary.get("HUMAN_REVIEW", 0)
                    )
                    exceptions = [
                        row
                        for row in topic_audit.get("articles", [])
                        if row.get("disposition") != "PASS"
                    ]
                    if exceptions:
                        st.warning(
                            "לא ניתן לאשר את הסל לפני החלפת הפסילות או הכרעה "
                            "לאחר שתי קריאות והכרעת ערעור אוטונומית במחלוקות."
                        )
                        st.dataframe(
                            [
                                {
                                    "כתבה": row.get("article_id"),
                                    "מצב": row.get("disposition"),
                                    "קריאה 1": (row.get("first_review") or {}).get("label"),
                                    "קריאה 2": (row.get("second_review") or {}).get("label"),
                                    "נימוק": (row.get("second_review") or {}).get("rationale"),
                                }
                                for row in exceptions
                            ],
                            hide_index=True,
                            width="stretch",
                        )
                    else:
                        st.success(
                            "כל 27 הכתבות עברו את שער התאמת הנושא: שתי קריאות "
                            "והכרעת ערעור אוטונומית רק במקום שנדרש."
                        )
            basket_reason = st.text_area(
                "הערה או דרישת שינוי לסל",
                key=f"basket-reason-{basket['basket_hash']}",
                placeholder="אפשר לציין כתבה או משבצת להחלפה. אישור מלא אינו מחייב הערה.",
            )
            basket_approve, basket_changes, basket_reject = st.columns(3)
            if basket_approve.button(
                "אישור הסל והקפאת הגרסה",
                type="primary",
                key=f"approve-basket-{basket['basket_hash']}",
                disabled=not audit_allows_approval,
            ):
                try:
                    decide_article_basket(
                        case_registry,
                        selected_basket_case,
                        decision="APPROVE",
                        actor="principal-researcher",
                        rationale=basket_reason,
                    )
                except (CaseRegistryError, ValueError) as error:
                    st.error(f"אישור הסל לא נשמר: {error}")
                else:
                    st.success("הסל הוקפא. הקייס מוכן לפייפליין המחקר.")
                    st.rerun()
            if basket_changes.button(
                "החזרה להשלמה/החלפה",
                key=f"change-basket-{basket['basket_hash']}",
                disabled=not basket_reason.strip(),
            ):
                try:
                    decide_article_basket(
                        case_registry,
                        selected_basket_case,
                        decision="REQUEST_CHANGES",
                        actor="principal-researcher",
                        rationale=basket_reason,
                    )
                except (CaseRegistryError, ValueError) as error:
                    st.error(f"דרישת השינוי לא נשמרה: {error}")
                else:
                    st.success("הסל הוחזר לגילוי מקומי. גרסה חדשה תידרש לאישור.")
                    st.rerun()
            if basket_reject.button(
                "דחיית הקייס",
                key=f"reject-basket-{basket['basket_hash']}",
                disabled=not basket_reason.strip(),
            ):
                try:
                    decide_article_basket(
                        case_registry,
                        selected_basket_case,
                        decision="REJECT",
                        actor="principal-researcher",
                        rationale=basket_reason,
                    )
                except (CaseRegistryError, ValueError) as error:
                    st.error(f"דחיית הקייס לא נשמרה: {error}")
                else:
                    st.success("הקייס נדחה בשער הסל ונשמר ביומן.")
                    st.rerun()

    with approved_tab:
        if not registered_cases:
            st.info("טרם אושר קייס חדש.")
        else:
            case_state_he = {
                "BASKET_DISCOVERY": "שער 1 אושר · הסל בבנייה",
                "AWAITING_BASKET_APPROVAL": "שער 1 אושר · הסל ממתין לאישור",
                "READY_TO_RUN": "שני השערים אושרו · מוכן למחקר",
                "RUNNING": "המחקר פעיל",
                "COMPLETED": "המחקר הושלם",
                "REJECTED": "נדחה",
                "FAILED": "נעצר בתקלה",
            }
            st.dataframe(
                [
                    {
                        "קייס": item["case_id"],
                        "נושא": item["title"],
                        "מצב": case_state_he.get(
                            item["current_state"], item["current_state"]
                        ),
                        "גרסת מתודולוגיה": item["methodology_version"],
                    }
                    for item in registered_cases
                ],
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "BASKET_DISCOVERY פירושו שהנושא אושר, אך טרם אושר סל כתבות "
                "והקייס עדיין אינו רשאי להיכנס לפייפליין הניתוח."
            )

    with rejected_tab:
        if not rejected_candidates:
            st.info("אין מועמדים שנדחו.")
        else:
            st.dataframe(
                [
                    {
                        "מזהה": item["candidate_id"],
                        "נושא": item["title"],
                        "זיקה": scope_labels.get(
                            item["scope_relation"], item["scope_relation"]
                        ),
                        "השפעת מדגם": item["sample_balance_impact"]["fills_slot"],
                    }
                    for item in rejected_candidates
                ],
                hide_index=True,
                width="stretch",
            )

    with selection_log_tab:
        selection_audit = case_registry.list_selection_audit()
        if not selection_audit:
            st.info("יומן הבחירה יתמלא לאחר החלטת הנושא הראשונה.")
        else:
            st.dataframe(
                [
                    {
                        "מועמד": item["candidate_id"],
                        "החלטה": "נכלל" if item["action"] == "INCLUDED" else "נדחה",
                        "נימוק": item["reason"],
                        "גרסת מתודולוגיה": item["methodology_version"],
                    }
                    for item in selection_audit
                ],
                hide_index=True,
                width="stretch",
            )

with queue_tab:
    st.subheader("תור העבודה הדטרמיניסטי")
    st.write(
        "התור נגזר מן הנתונים הקיימים ואינו מפעיל מודל. תיקים פתוחים היו "
        f"מופיעים לפני היסטוריה סגורה; כעת נותרו "
        f"{sum(row['status'] != 'DECIDED' for row in queue_rows)} תיקים פתוחים."
    )
    queue_status = st.multiselect(
        "מצב בתור",
        options=["PENDING", "DEFERRED", "DECIDED"],
        default=["PENDING", "DEFERRED", "DECIDED"],
        format_func=lambda value: STATUS_HE[value],
    )
    shown_queue = [row for row in queue_rows if row["status"] in queue_status]
    st.dataframe(
        [
            {
                "קדימות": row["priority_reason_he"],
                "מצב": STATUS_HE[row["status"]],
                "מקור": row["source"],
                "שפה": LANGUAGE_LABELS_HE[row["language"]],
                "שלב": PHASE_HE[row["sample_phase"]],
                "מדד": row["component"],
                "הצעת מודל": score_label(row["model_score"]),
                "ביטחון": row["model_confidence"],
            }
            for row in shown_queue
        ],
        hide_index=True,
        width="stretch",
    )

with court_tab:
    apply_staged_court_navigation(st.session_state)
    matching_cases = [
        dilemma for dilemma in material.dilemmas
        if (dilemma["article"].get("source_name") or dilemma["source_id"]) in source_filter
        and dilemma["component"] in component_filter
    ]
    actionable_cases, decided_cases = split_dilemmas_by_status(
        matching_cases, current
    )
    st.subheader("אולם הדיונים")
    list_view = st.radio(
        "אזור עבודה",
        options=["ACTIONABLE", "DECIDED"],
        format_func=lambda value: (
            f"דורשים טיפול · {len(actionable_cases)}"
            if value == "ACTIONABLE"
            else f"היסטוריה שהוכרעה · {len(decided_cases)}"
        ),
        horizontal=True,
        key="court_list_view",
    )
    flash_message = st.session_state.pop("court_flash_message", None)
    if flash_message:
        st.success(flash_message)

    case_pool = actionable_cases if list_view == "ACTIONABLE" else decided_cases
    if not case_pool:
        if not matching_cases:
            st.info("אין דיונים שתואמים למסנני המקור והמדד שנבחרו.")
        elif list_view == "ACTIONABLE":
            st.success("אין דיונים שממתינים לטיפול. כל הדיונים התואמים הוכרעו.")
            st.caption("פסקי הדין הקיימים זמינים בנפרד באזור ‘היסטוריה שהוכרעה’.")
        else:
            st.info("עדיין אין דיונים שהוכרעו עבור המסננים שנבחרו.")
    else:
        def format_case(dilemma):
            source = dilemma["article"].get("source_name") or dilemma["source_id"]
            status = STATUS_HE[decision_status(current.get(dilemma["dilemma_id"]))]
            return f"{source} · {dilemma['component']} · {status}"

        case_index = {item["dilemma_id"]: item for item in case_pool}
        document_index = {}
        for item in case_pool:
            document_index.setdefault(item["document_id"], []).append(item)
        st.caption(
            f"{len(document_index)} תיקי כתבה · {len(case_pool)} רכיבים להכרעה. "
            "רכיבים הנובעים מאותה מחלוקת מוצגים יחד באותו תיק."
        )
        selection_key = (
            "court_actionable_case_id"
            if list_view == "ACTIONABLE"
            else "court_decided_case_id"
        )
        document_selection_key = (
            "court_actionable_document_id"
            if list_view == "ACTIONABLE"
            else "court_decided_document_id"
        )
        staged_case_id = (
            st.session_state.pop(
                "_court_actionable_document_sync_case_id", None
            )
            if list_view == "ACTIONABLE"
            else None
        )
        if staged_case_id in case_index:
            st.session_state[document_selection_key] = case_index[staged_case_id][
                "document_id"
            ]
        document_ids = list(document_index)
        if st.session_state.get(document_selection_key) not in document_ids:
            st.session_state[document_selection_key] = document_ids[0]

        def format_document(document_id):
            items = document_index[document_id]
            article = items[0]["article"]
            source = article.get("source_name") or items[0]["source_id"]
            title = article.get("title") or "ללא כותרת"
            return f"{source} · {title} · {len(items)} רכיבים"

        selected_document_id = st.selectbox(
            (
                "בחירת תיק כתבה שדורש טיפול"
                if list_view == "ACTIONABLE"
                else "בחירת תיק כתבה שהוכרע"
            ),
            document_ids,
            format_func=format_document,
            key=document_selection_key,
        )
        case_ids = [
            item["dilemma_id"] for item in document_index[selected_document_id]
        ]
        if st.session_state.get(selection_key) not in case_ids:
            st.session_state[selection_key] = case_ids[0]
        selected_id = st.selectbox(
            (
                "בחירת רכיב במחלוקת בתוך התיק"
                if list_view == "ACTIONABLE"
                else "בחירת רכיב שהוכרע בתוך התיק"
            ),
            case_ids,
            format_func=lambda dilemma_id: format_case(case_index[dilemma_id]),
            key=selection_key,
        )
        selected = case_index[selected_id]
        article = selected["article"]
        active_decision = current.get(selected["dilemma_id"])
        status = decision_status(active_decision)

        st.subheader(f"{selected['component']} · {COMPONENT_LABELS[selected['component']]}")
        st.markdown(
            f"<div class='case-meta'>{html.escape(article.get('source_name') or selected['source_id'])} · "
            f"{html.escape(article.get('published_at', ''))} · "
            f"{html.escape(PHASE_HE.get(selected['sample_phase'], selected['sample_phase']))} · "
            f"מצב: <b>{STATUS_HE[status]}</b></div>",
            unsafe_allow_html=True,
        )
        st.markdown(f"### השאלה שבמחלוקת\n{selected['question_he']}")
        st.info(selected["bench_note_he"], icon="🧭")
        issue_labels = [
            ISSUE_TAGS_HE.get(tag, tag) for tag in selected.get("issue_tags", [])
        ]
        st.caption("סוגיות משפטיות: " + " · ".join(issue_labels))

        relevant_precedents = find_relevant_precedents(selected, precedents)
        if relevant_precedents:
            has_binding = any(
                item["precedent_mode"] == "BINDING_POLICY"
                for item in relevant_precedents
            )
            with st.expander(
                f"תקדימים רלוונטיים ממקרים קודמים · {len(relevant_precedents)}",
                expanded=has_binding,
            ):
                if has_binding:
                    st.warning(
                        "נמצאה מדיניות מחייבת דומה. זו המלצה עקבית בלבד; "
                        "המערכת אינה שומרת פסק דין במקומך."
                    )
                for precedent in relevant_precedents[:3]:
                    with st.container(border=True):
                        st.markdown(
                            f"**{PRECEDENT_MODE_HE[precedent['precedent_mode']]} · "
                            f"{precedent['source']} · {precedent['component']}="
                            f"{score_label(precedent['final_score'], precedent['final_na_reason'])}**"
                        )
                        st.write(precedent["policy_rule"])
                        if precedent.get("policy_warning_he"):
                            st.warning(precedent["policy_warning_he"])
                        shared = [
                            ISSUE_TAGS_HE.get(tag, tag)
                            for tag in precedent["shared_tags"]
                        ]
                        st.caption("חפיפה: " + " · ".join(shared))

        evidence = selected.get("decisive_exhibit")
        if evidence:
            translation = selected.get("evidence_translation")
            if selected["language"] != "he" and translation:
                st.markdown("#### המוצג המכריע · תרגום לעברית")
                st.markdown(
                    f"<div class='translation' dir='rtl'>{html.escape(translation['translation_he'])}</div>",
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"תרגום מחקרי מתועד · "
                    f"{TRANSLATION_STATUS_HE.get(translation['review_status'], translation['review_status'])} · "
                    f"{translation['translation_id']} · "
                    f"מיקום במקור: {evidence.get('location', 'לא צוין')}"
                )
                st.write(translation["translation_note_he"])
                with st.expander("הצגת הקטע בשפת המקור"):
                    st.markdown(
                        f"<div class='evidence' dir='auto'>{html.escape(evidence.get('quote_original', ''))}</div>",
                        unsafe_allow_html=True,
                    )
                st.markdown("##### דף פירושים")
                for term in translation["glossary"]:
                    st.caption(
                        f"**{term['term_he']}** · `{term['source_term']}` — "
                        f"{term['explanation_he']}"
                    )
            elif selected["language"] != "he":
                st.error("לקטע זה עדיין אין תרגום עברי מתועד. אין להכריע בתיק לפני השלמת התרגום.")
                with st.expander("הצגת הקטע בשפת המקור"):
                    st.markdown(
                        f"<div class='evidence' dir='auto'>{html.escape(evidence.get('quote_original', ''))}</div>",
                        unsafe_allow_html=True,
                    )
            else:
                st.markdown("#### המוצג המכריע")
                st.markdown(
                    f"<div class='evidence' dir='auto'>{html.escape(evidence.get('quote_original', ''))}</div>",
                    unsafe_allow_html=True,
                )
                st.caption(f"מיקום: {evidence.get('location', 'לא צוין')}")
            if evidence.get("curation_note_he"):
                st.caption(evidence["curation_note_he"])
        else:
            if selected.get("visual_exhibits"):
                st.info(
                    "המודל לא החזיר קטע טקסט מכריע. בתיק זה הראיה שבמחלוקת "
                    "היא חזותית, והיא מוצגת להלן."
                )
            else:
                st.warning("המודל לא החזיר קטע מכריע עבור רכיב זה — זה חלק מן הסיבה לדיון.")

        if selected.get("visual_exhibits"):
            st.markdown("#### המוצגים החזותיים שבמחלוקת")
            st.caption(
                "הנגן מפנה ישירות לקובץ אצל המפרסם; קובצי הווידאו אינם "
                "נטענים לזיכרון של ALETHEIA ואינם מועתקים למסד המקומי."
            )
            for visual_exhibit in selected["visual_exhibits"]:
                show_visual_exhibit(visual_exhibit)

        left, right = st.columns(2, gap="large")
        with left:
            with st.container(border=True):
                st.markdown("### טיעון א · הצעת Vertex")
                st.metric("הציון המוצע", score_label(selected["model"].get("score")))
                st.caption(f"ביטחון המודל: {selected['model'].get('confidence') or 'לא דורג'}")
                st.write(selected["model"].get("rationale", "לא סופק נימוק."))
                reasons = selected["model"].get("reasons", [])
                if reasons:
                    st.caption("סיבת הניתוב: " + ", ".join(reasons))
        with right:
            with st.container(border=True):
                st.markdown("### טיעון ב · ההשגה המתודולוגית")
                st.metric(
                    "הציון החלופי",
                    score_label(selected["challenge"].get("score"), selected["challenge"].get("na_reason")),
                )
                st.caption(selected["challenge"]["label_he"])
                st.write(selected["challenge"]["argument_he"])

        with st.expander(
            "ספר החוקים והעוגנים הרלוונטיים",
            expanded=bool(selected.get("anchor_review")),
        ):
            st.markdown(f"#### סולם {selected['component']}")
            st.table([{"ציון": score, "משמעות": meaning} for score, meaning in SCORE_LADDERS[selected["component"]]])
            anchor_reviews = {
                item["anchor_id"]: item
                for item in selected.get("anchor_review", [])
            }
            formal_anchor_refs = list(dict.fromkeys(
                selected["model"].get("anchor_refs", [])
                + selected["challenge"].get("anchor_refs", [])
            ))
            if anchor_reviews and not formal_anchor_refs:
                st.warning(
                    "אין עוגן פעיל ותואם־טענה במועד הפרסום. העוגנים שלהלן "
                    "מוצגים כדי להבין את התשתית: הם הקשריים או מאוחרים, ולכן "
                    "אינם מאפשרים לבדם ניקוד T1 לפי הכלל הקפוא."
                )
            if selected["anchors"]:
                st.markdown("#### עוגנים")
                for anchor in selected["anchors"]:
                    show_anchor(anchor, anchor_reviews.get(anchor["anchor_id"]))
                    st.divider()
            else:
                st.caption("לא צורף עוגן תואם-טענה לתיק זה.")

        with st.expander("הכתבה המלאה והמטא־דאטה"):
            st.markdown(f"**{article.get('title', 'ללא כותרת')}**")
            if article.get("url"):
                st.link_button("פתיחת המקור הציבורי", article["url"])
            st.text_area("טקסט שנשמר למחקר", article.get("text", ""), height=320, disabled=True)

        st.divider()
        st.markdown("### פסק הדין שלך")
        if active_decision:
            st.write(
                f"החלטה נוכחית: **{OUTCOME_HE[active_decision['outcome']]}** · "
                f"ציון אפקטיבי **{score_label(active_decision['final_score'], active_decision['final_na_reason'])}** · "
                f"גרסה {active_decision['decision_version']}"
            )
            st.caption("שמירה נוספת אינה מוחקת החלטה קודמת; היא יוצרת גרסה חדשה שמחליפה אותה כתוצאה הנוכחית.")

        with st.form(f"decision-{selected['dilemma_id']}", clear_on_submit=False):
            outcome_label = st.selectbox(
                "הכרעה",
                [
                    "בחר/י הכרעה",
                    "קבלת טיעון א — הצעת Vertex",
                    "קבלת טיעון ב — ההשגה המתודולוגית",
                    "פסק דין עצמאי",
                    "דחיית ההכרעה לעת עתה",
                ],
            )
            custom_value = st.selectbox(
                "ציון עצמאי — חל רק אם נבחר ‘פסק דין עצמאי’",
                ["0", "1", "2", "3", "4", "N/A"], index=2,
            )
            custom_na_reason = st.text_input("נימוק טכני ל-N/A, אם נבחר")
            rationale = st.text_area(
                "נימוק פסק הדין", placeholder="מה הכריע בין הטיעונים, ואיזה כלל הופעל?", height=120,
            )
            precedent_label = st.selectbox(
                "השפעה על תיקים עתידיים",
                [
                    "מקרה נקודתי — לא יוכל לשמש תקדים",
                    "תקדים מנחה — יוצג במקרים דומים",
                    "מדיניות מחייבת — תוצע ככלל עד שינוי מפורש",
                ],
            )
            policy_rule = st.text_area(
                "הכלל שיישמר בספר התקדימים",
                value="",
                placeholder="אפשר להשאיר ריק: המערכת תנסח כלל מן ההכרעה והנימוק.",
                help=(
                    "כלל מפורש נדרש רק למדיניות מחייבת. אם השדה נשאר ריק, "
                    "יישמר כלל שתואם להכרעה שבחרת ולנימוק שלך."
                ),
                height=100,
            )
            st.caption(
                "תחום התחולה ייקבע לפי הסוגיות: " + " · ".join(issue_labels)
            )
            decided_by = st.text_input("שם או תפקיד השופט", value="החוקר הראשי")
            submitted = st.form_submit_button("שמירת פסק הדין", type="primary")
            if submitted:
                outcome_map = {
                    "קבלת טיעון א — הצעת Vertex": "ACCEPT_MODEL",
                    "קבלת טיעון ב — ההשגה המתודולוגית": "ACCEPT_CHALLENGE",
                    "פסק דין עצמאי": "CUSTOM",
                    "דחיית ההכרעה לעת עתה": "DEFER",
                }
                precedent_mode_map = {
                    "תקדים מנחה — יוצג במקרים דומים": "PERSUASIVE_PRECEDENT",
                    "מקרה נקודתי — לא יוכל לשמש תקדים": "CASE_ONLY",
                    "מדיניות מחייבת — תוצע ככלל עד שינוי מפורש": "BINDING_POLICY",
                }
                if outcome_label not in outcome_map:
                    st.error("יש לבחור הכרעה לפני השמירה.")
                else:
                    try:
                        store.save_decision(
                            selected,
                            outcome=outcome_map[outcome_label],
                            rationale=rationale,
                            decided_by=decided_by,
                            custom_score=None if custom_value == "N/A" else int(custom_value),
                            custom_is_na=custom_value == "N/A",
                            custom_na_reason=custom_na_reason,
                            precedent_mode=precedent_mode_map[precedent_label],
                            policy_rule=policy_rule,
                            policy_scope=selected.get("issue_tags", []),
                        )
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        updated_decisions = store.current_decisions()
                        next_dilemma_id = next_actionable_dilemma_id(
                            matching_cases,
                            updated_decisions,
                            current_dilemma_id=selected["dilemma_id"],
                        )
                        stage_court_navigation(
                            st.session_state, next_dilemma_id
                        )
                        if next_dilemma_id is None:
                            st.session_state["court_flash_message"] = (
                                "פסק הדין נשמר ועודכן בספר התקדימים. "
                                "לא נותרו דיונים שממתינים לטיפול."
                            )
                        else:
                            st.session_state["court_flash_message"] = (
                                "פסק הדין נשמר ועודכן בספר התקדימים. "
                                "עברנו אוטומטית לדיון הבא שדורש התייחסות."
                            )
                        st.rerun()

        history = store.history(selected["dilemma_id"])
        if history:
            with st.expander(f"היסטוריית התיק · {len(history)} גרסאות"):
                for item in history:
                    current_mark = " · נוכחית" if item["is_current"] else ""
                    st.markdown(
                        f"**גרסה {item['decision_version']}{current_mark}** — "
                        f"{OUTCOME_HE[item['outcome']]} · ציון "
                        f"{score_label(item['final_score'], item['final_na_reason'])}"
                    )
                    st.caption(f"{item['decided_by']} · {item['decided_at']}")
                    st.write(item["rationale"])
                    st.caption(
                        f"השפעה עתידית: "
                        f"{PRECEDENT_MODE_HE.get(item.get('precedent_mode'), 'תקדים מנחה')}"
                    )
                    if item.get("policy_rule"):
                        st.write("כלל: " + item["policy_rule"])

with case2_court_tab:
    court_case_id = st.radio(
        "בחירת קייס לשיפוט",
        [
            "CASE-002",
            "CASE-003",
            "CASE-004",
            "CASE-005",
            "CASE-006",
            "CASE-007",
            "CASE-008",
        ],
        horizontal=True,
        key="multicase-court-case",
    )
    court_config = {
        "CASE-002": {
            "title": "תקיפת שיירת WCK",
            "review": CASE2_REVIEW_PATH,
            "database": CASE2_DATABASE_PATH,
        },
        "CASE-003": {
            "title": "חיסול חסן נסראללה",
            "review": CASE3_REVIEW_PATH,
            "database": CASE3_DATABASE_PATH,
        },
        "CASE-004": {
            "title": "מתקפת הטילים והכטב״מים האיראנית הראשונה",
            "review": CASE4_REVIEW_PATH,
            "database": CASE4_DATABASE_PATH,
        },
        "CASE-005": {
            "title": "צו הביניים של בית הדין הבינלאומי לצדק בעניין עזה",
            "review": CASE5_REVIEW_PATH,
            "database": CASE5_DATABASE_PATH,
        },
        "CASE-006": {
            "title": "חיסול יחיא סינוואר",
            "review": CASE6_REVIEW_PATH,
            "database": CASE6_DATABASE_PATH,
        },
        "CASE-007": {
            "title": "תחילת ההפוגה ושחרור החטופים הראשוני",
            "review": CASE7_REVIEW_PATH,
            "database": CASE7_DATABASE_PATH,
        },
        "CASE-008": {
            "title": "מבצע חילוץ החטופים בנוסייראת",
            "review": CASE8_REVIEW_PATH,
            "database": CASE8_DATABASE_PATH,
        },
    }[court_case_id]
    court_key = court_case_id.lower().replace("-", "")
    court_review_path = court_config["review"]
    st.subheader(f"{court_case_id} · {court_config['title']}")
    if not court_review_path.exists():
        st.info(f"{court_case_id} עדיין לא הגיע לשלב שיפוט אנושי.")
    else:
        case2_payload = get_case2_review(
            str(court_review_path),
            court_review_path.stat().st_size,
            court_review_path.stat().st_mtime_ns,
        )
        case2_store = get_multicase_store(str(court_config["database"]))
        case2_decisions = case2_store.current_decisions()
        case2_dilemmas = case2_payload["human_dilemmas"]
        case2_open, case2_decided = split_dilemmas_by_status(
            case2_dilemmas, case2_decisions
        )
        summary = case2_payload["summary"]
        metric_a, metric_b, metric_c, metric_d = st.columns(4)
        metric_a.metric("רכיבים שנותחו", 216)
        metric_b.metric("עברו ביקורת שנייה", summary["reviewed_dilemmas"])
        metric_c.metric(
            "נסגרו אוטומטית",
            summary["auto_resolved"] + 216 - summary["reviewed_dilemmas"],
        )
        metric_d.metric("דורשים אדם", len(case2_open))
        st.caption(
            f"27 כתבות × 8 רכיבים. {216 - summary['reviewed_dilemmas']} רכיבים "
            f"נסגרו לפני הערעור הכפול ועוד {summary['auto_resolved']} נסגרו "
            "אוטומטית אחריו. N/A מול מספר נסגר אוטומטית רק כאשר שתי הצבעות "
            "חזקות ובלתי־תלויות מסכימות; כאן מוצגים רק חריגים קשים שנותרו."
        )
        case2_view = st.radio(
            f"אזור עבודה ב־{court_case_id}",
            ["OPEN", "DECIDED"],
            format_func=lambda value: (
                f"דורשים טיפול · {len(case2_open)}"
                if value == "OPEN" else f"היסטוריה שהוכרעה · {len(case2_decided)}"
            ),
            horizontal=True,
            key=f"{court_key}-court-view",
        )
        pool = case2_open if case2_view == "OPEN" else case2_decided
        if not pool:
            if case2_view == "OPEN":
                st.success(f"כל ההכרעות החריגות של {court_case_id} הושלמו.")
            else:
                st.info(f"עדיין אין הכרעות שמורות ב־{court_case_id}.")
        else:
            by_id = {row["dilemma_id"]: row for row in pool}

            def case2_label(dilemma_id):
                row = by_id[dilemma_id]
                return (
                    f"{row['article']['source_name']} · {row['component']} · "
                    f"{row['article']['title']}"
                )

            selected_id = st.selectbox(
                "בחירת דיון — ניתן לעבור בחופשיות בין כל התיקים",
                list(by_id),
                format_func=case2_label,
                key=f"{court_key}-selected-{case2_view}",
            )
            selected = by_id[selected_id]
            article = selected["article"]
            active = case2_decisions.get(selected_id)
            st.markdown(
                f"### {selected['component']} · "
                f"{COMPONENT_LABELS[selected['component']]}"
            )
            st.caption(
                f"{article['source_name']} · {article['published_at']} · "
                f"{PHASE_HE.get(selected['sample_phase'], selected['sample_phase'])}"
            )
            st.markdown(f"#### השאלה שבמחלוקת\n{selected['question_he']}")
            st.info(selected["bench_note_he"], icon="🧭")

            evidence = selected.get("decisive_exhibit")
            translation = selected.get("evidence_translation")
            if evidence:
                st.markdown("#### המוצג המכריע")
                if selected["language"] != "he" and translation:
                    st.markdown(
                        f"<div class='translation' dir='rtl'>"
                        f"{html.escape(translation['translation_he'])}</div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(translation.get("translation_note_he", ""))
                    with st.expander("הצגת המקור והפירושים"):
                        st.markdown(
                            f"<div class='evidence' dir='auto'>"
                            f"{html.escape(evidence['quote_original'])}</div>",
                            unsafe_allow_html=True,
                        )
                        for term in translation.get("glossary", []):
                            st.caption(
                                f"**{term['term_he']}** · `{term['source_term']}` — "
                                f"{term['explanation_he']}"
                            )
                else:
                    st.markdown(
                        f"<div class='evidence' dir='auto'>"
                        f"{html.escape(evidence['quote_original'])}</div>",
                        unsafe_allow_html=True,
                    )

            left, right = st.columns(2, gap="large")
            with left:
                with st.container(border=True):
                    st.markdown("### טיעון א · הניתוח הראשוני")
                    st.metric(
                        "ציון",
                        score_label(
                            selected["model"].get("score"),
                            selected["model"].get("na_reason"),
                        ),
                    )
                    st.write(selected["model"].get("rationale", ""))
            with right:
                with st.container(border=True):
                    st.markdown("### טיעון ב · ההשגה המתודולוגית")
                    st.metric(
                        "ציון חלופי",
                        score_label(
                            selected["challenge"].get("score"),
                            selected["challenge"].get("na_reason"),
                        ),
                    )
                    st.write(selected["challenge"]["argument_he"])

            review = selected["second_review"]
            with st.expander("מה קבעה הביקורת העיוורת הכפולה?", expanded=True):
                ballot_a = review["ballot_1"]
                ballot_b = review["ballot_2"]
                st.write(
                    "הסדר הוחלף בין שתי ההצבעות כדי לבדוק הטיית מיקום. "
                    f"פתק 1: **{score_label(ballot_a.get('proposed_score'), ballot_a.get('proposed_na_reason'))}** · "
                    f"פתק 2: **{score_label(ballot_b.get('proposed_score'), ballot_b.get('proposed_na_reason'))}**."
                )
                if review["order_stable"]:
                    st.success("שתי ההצבעות הגיעו לאותה תוצאה, אך הפער מן הניתוח הראשוני מחייב הכרעת אדם.")
                else:
                    st.warning("התוצאה השתנתה עם סדר הטיעונים; בגלל פער קשה התיק הועבר אליך.")

            with st.expander("סולם הניקוד, העוגנים והכתבה המלאה"):
                st.table([
                    {"ציון": score, "משמעות": meaning}
                    for score, meaning in SCORE_LADDERS[selected["component"]]
                ])
                for anchor in selected.get("anchors", []):
                    show_anchor(anchor)
                    st.divider()
                st.markdown(f"**{article['title']}**")
                st.link_button("פתיחת המקור הציבורי", article["url"])
                st.text_area(
                    "הטקסט שנשמר למחקר",
                    article.get("text", ""),
                    height=320,
                    disabled=True,
                    key=f"{court_key}-article-{selected_id}",
                )

            if active:
                st.success(
                    f"הכרעה נוכחית: {OUTCOME_HE[active['outcome']]} · "
                    f"ציון {score_label(active['final_score'], active['final_na_reason'])}"
                )
            with st.form(f"{court_key}-decision-{selected_id}"):
                outcome_label = st.selectbox(
                    "הכרעה",
                    [
                        "בחר/י הכרעה",
                        "קבלת טיעון א — הניתוח הראשוני",
                        "קבלת טיעון ב — ההשגה המתודולוגית",
                        "פסק דין עצמאי",
                        "דחיית ההכרעה לעת עתה",
                    ],
                )
                custom_value = st.selectbox("ציון עצמאי", ["0", "1", "2", "3", "4", "N/A"], index=2)
                custom_na_reason = st.text_input("נימוק טכני ל־N/A, אם נבחר")
                rationale = st.text_area("נימוק פסק הדין", height=120)
                precedent_mode_label = st.selectbox(
                    "השפעה עתידית",
                    ["מקרה נקודתי", "תקדים מנחה", "מדיניות מחייבת"],
                )
                policy_rule = st.text_area("כלל מדיניות מפורש, אם נדרש", height=80)
                submitted = st.form_submit_button("שמירת פסק הדין", type="primary")
                if submitted:
                    outcome_map = {
                        "קבלת טיעון א — הניתוח הראשוני": "ACCEPT_MODEL",
                        "קבלת טיעון ב — ההשגה המתודולוגית": "ACCEPT_CHALLENGE",
                        "פסק דין עצמאי": "CUSTOM",
                        "דחיית ההכרעה לעת עתה": "DEFER",
                    }
                    precedent_map = {
                        "מקרה נקודתי": "CASE_ONLY",
                        "תקדים מנחה": "PERSUASIVE_PRECEDENT",
                        "מדיניות מחייבת": "BINDING_POLICY",
                    }
                    if outcome_label not in outcome_map:
                        st.error("יש לבחור הכרעה לפני השמירה.")
                    else:
                        try:
                            case2_store.save_decision(
                                selected,
                                outcome_map[outcome_label],
                                rationale,
                                "החוקר הראשי",
                                custom_score=(None if custom_value == "N/A" else int(custom_value)),
                                custom_is_na=custom_value == "N/A",
                                custom_na_reason=custom_na_reason,
                                precedent_mode=precedent_map[precedent_mode_label],
                                policy_rule=policy_rule,
                            )
                        except ValueError as error:
                            st.error(str(error))
                        else:
                            st.success("פסק הדין נשמר. עוברים לדיון הבא שנותר.")
                            st.rerun()


with t4_tab:
    st.subheader("T4 · תיקון ועדכון לאורך זמן")
    st.write(
        "T4 נמדד על רצף שלוש הכתבות באותה משבצת מקור. הוא אינו נגזר "
        "מכתבה יחידה ואינו נקבע אוטומטית על ידי Vertex. שתי המשבצות "
        "המפוצלות קיבלו N/A לפי הכלל המתודולוגי שכבר אושר."
    )
    t4_a, t4_b, t4_c = st.columns(3)
    t4_a.metric("מסלולים", len(t4_trajectories))
    t4_b.metric("הוכרעו", len(current_t4))
    t4_c.metric("ממתינים", len(t4_readiness["pending_trajectory_ids"]))

    pending_t4 = [
        item for item in t4_trajectories
        if item["trajectory_id"] not in current_t4
    ]
    if "_t4_selected_next" in st.session_state:
        st.session_state["t4_selected_id"] = st.session_state.pop(
            "_t4_selected_next"
        )
    trajectory_pool = pending_t4 or t4_trajectories
    trajectory_index = {
        item["trajectory_id"]: item for item in trajectory_pool
    }
    trajectory_ids = list(trajectory_index)
    if st.session_state.get("t4_selected_id") not in trajectory_ids:
        st.session_state["t4_selected_id"] = trajectory_ids[0]

    def format_trajectory(trajectory_id):
        item = trajectory_index[trajectory_id]
        sources = " / ".join(item["source_names"])
        state = "הוכרע" if trajectory_id in current_t4 else "ממתין"
        return f"{item['language_he']} · {sources} · {state}"

    selected_t4_id = st.selectbox(
        "בחירת מסלול מקור",
        trajectory_ids,
        format_func=format_trajectory,
        key="t4_selected_id",
    )
    selected_t4 = trajectory_index[selected_t4_id]
    proposal = selected_t4["proposal"]
    active_t4 = current_t4.get(selected_t4_id)

    if active_t4:
        st.success(
            "הכרעה נוכחית: "
            + score_label(active_t4["score"], active_t4["na_reason"])
            + f" · גרסה {active_t4['decision_version']}"
        )
        st.write(active_t4["rationale"])
        st.caption(f"{active_t4['decided_by']} · {active_t4['decided_at']}")

    st.markdown("### שלוש הכתבות במסלול")
    article_columns = st.columns(3, gap="large")
    for column, article in zip(article_columns, selected_t4["articles"]):
        with column:
            with st.container(border=True):
                st.markdown(f"**{article['phase_he']}**")
                st.caption(
                    f"{article['source_name']} · {article['published_at']}"
                )
                st.write(article["title"])
                focus = article["focus_components"]
                st.caption(
                    " · ".join(
                        f"{component}={score_label(focus[component]['score'])}"
                        for component in ("T1", "T3")
                    )
                )
                for component in ("T1", "T3"):
                    item = focus[component]
                    with st.expander(f"{component} · הנימוק והמוצג"):
                        st.write(item["rationale"])
                        evidence = item.get("evidence_span") or {}
                        if evidence.get("quote_original"):
                            st.markdown(
                                "<div class='evidence' dir='auto'>"
                                + html.escape(evidence["quote_original"])
                                + "</div>",
                                unsafe_allow_html=True,
                            )

    st.markdown("### המלצת המחקר")
    recommendation_label = score_label(
        proposal["proposed_score"], proposal["proposed_na_reason"]
    )
    st.metric("T4 מוצע", recommendation_label)
    st.write(proposal["rationale_he"])
    if proposal.get("uncertainty_he"):
        st.warning(proposal["uncertainty_he"])

    if selected_t4["split_slot"]:
        st.info(
            "המסלול מפוצל בין שני גופים ולכן הוכרע אוטומטית כ־N/A "
            "מכוח הכלל שאושר."
        )
    else:
        with st.form(f"t4-decision-{selected_t4_id}"):
            t4_outcome = st.selectbox(
                "הכרעה",
                ["קבלת ההמלצה", "ציון עצמאי", "N/A"],
            )
            t4_custom_score = st.selectbox(
                "ציון עצמאי",
                [0, 1, 2, 3, 4],
                index=(proposal["proposed_score"] or 0),
            )
            t4_na_reason = st.text_input(
                "סיבת N/A",
                value="NO_GENUINE_UPDATE_OPPORTUNITY"
                if t4_outcome == "N/A" else "",
            )
            t4_rationale = st.text_area(
                "נימוק ההכרעה",
                value=proposal["rationale_he"],
                height=140,
            )
            t4_decided_by = st.text_input(
                "שם או תפקיד השופט",
                value="החוקר הראשי",
                key=f"t4-judge-{selected_t4_id}",
            )
            t4_submitted = st.form_submit_button(
                "שמירת הכרעת T4", type="primary"
            )
            if t4_submitted:
                if t4_outcome == "קבלת ההמלצה":
                    final_t4_score = proposal["proposed_score"]
                    final_t4_na_reason = proposal["proposed_na_reason"]
                elif t4_outcome == "ציון עצמאי":
                    final_t4_score = t4_custom_score
                    final_t4_na_reason = None
                else:
                    final_t4_score = None
                    final_t4_na_reason = t4_na_reason
                try:
                    store.save_trajectory_decision(
                        selected_t4,
                        score=final_t4_score,
                        na_reason=final_t4_na_reason,
                        rationale=t4_rationale,
                        decided_by=t4_decided_by,
                    )
                except ValueError as error:
                    st.error(str(error))
                else:
                    updated_t4 = store.current_trajectory_decisions()
                    next_t4 = next(
                        (
                            item["trajectory_id"]
                            for item in t4_trajectories
                            if item["trajectory_id"] not in updated_t4
                        ),
                        selected_t4_id,
                    )
                    st.session_state["_t4_selected_next"] = next_t4
                    subprocess.run(
                        [
                            sys.executable,
                            str(BASE_DIR / "scripts/finalize_case001_results.py"),
                        ],
                        cwd=BASE_DIR,
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    st.rerun()

    t4_history = store.trajectory_history(selected_t4_id)
    if t4_history:
        with st.expander(f"היסטוריית המסלול · {len(t4_history)} גרסאות"):
            for item in t4_history:
                current_mark = " · נוכחית" if item["is_current"] else ""
                st.markdown(
                    f"**גרסה {item['decision_version']}{current_mark}** — "
                    f"{score_label(item['score'], item['na_reason'])}"
                )
                st.caption(f"{item['decided_by']} · {item['decided_at']}")
                st.write(item["rationale"])

with dashboards_tab:
    st.subheader("דשבורדים מחקריים")
    case1_payload, case1_score_records = get_score_records(
        str(CASE1_RESULTS_PATH),
        CASE1_RESULTS_PATH.stat().st_size,
        CASE1_RESULTS_PATH.stat().st_mtime_ns,
    )
    case2_payload, case2_score_records = get_score_records(
        str(CASE2_RESULTS_PATH),
        CASE2_RESULTS_PATH.stat().st_size,
        CASE2_RESULTS_PATH.stat().st_mtime_ns,
    )
    case3_payload, case3_score_records = get_score_records(
        str(CASE3_RESULTS_PATH),
        CASE3_RESULTS_PATH.stat().st_size,
        CASE3_RESULTS_PATH.stat().st_mtime_ns,
    )
    case4_payload, case4_score_records = get_score_records(
        str(CASE4_RESULTS_PATH),
        CASE4_RESULTS_PATH.stat().st_size,
        CASE4_RESULTS_PATH.stat().st_mtime_ns,
    )
    case1_frame_payload = get_json_payload(
        str(CASE1_FRAME_RESULTS_PATH),
        CASE1_FRAME_RESULTS_PATH.stat().st_size,
        CASE1_FRAME_RESULTS_PATH.stat().st_mtime_ns,
    )
    case2_frame_payload = get_json_payload(
        str(CASE2_FRAME_RESULTS_PATH),
        CASE2_FRAME_RESULTS_PATH.stat().st_size,
        CASE2_FRAME_RESULTS_PATH.stat().st_mtime_ns,
    )
    case3_frame_payload = get_json_payload(
        str(CASE3_FRAME_RESULTS_PATH),
        CASE3_FRAME_RESULTS_PATH.stat().st_size,
        CASE3_FRAME_RESULTS_PATH.stat().st_mtime_ns,
    )
    case4_frame_payload = get_json_payload(
        str(CASE4_FRAME_RESULTS_PATH),
        CASE4_FRAME_RESULTS_PATH.stat().st_size,
        CASE4_FRAME_RESULTS_PATH.stat().st_mtime_ns,
    )
    case3_closeout = get_json_payload(
        str(CASE3_CLOSEOUT_PATH),
        CASE3_CLOSEOUT_PATH.stat().st_size,
        CASE3_CLOSEOUT_PATH.stat().st_mtime_ns,
    )
    case4_closeout = get_json_payload(
        str(CASE4_CLOSEOUT_PATH),
        CASE4_CLOSEOUT_PATH.stat().st_size,
        CASE4_CLOSEOUT_PATH.stat().st_mtime_ns,
    )
    case5_article_summary = get_json_payload(
        str(CASE5_ARTICLE_SUMMARY_PATH),
        CASE5_ARTICLE_SUMMARY_PATH.stat().st_size,
        CASE5_ARTICLE_SUMMARY_PATH.stat().st_mtime_ns,
    )
    case5_frame_second_summary = get_json_payload(
        str(CASE5_FRAME_SECOND_SUMMARY_PATH),
        CASE5_FRAME_SECOND_SUMMARY_PATH.stat().st_size,
        CASE5_FRAME_SECOND_SUMMARY_PATH.stat().st_mtime_ns,
    )
    case5_review_payload = get_json_payload(
        str(CASE5_REVIEW_PATH),
        CASE5_REVIEW_PATH.stat().st_size,
        CASE5_REVIEW_PATH.stat().st_mtime_ns,
    )
    case5_score_records = None
    case5_frame_payload = None
    case5_closeout = None
    if CASE5_RESULTS_PATH.exists() and CASE5_FRAME_RESULTS_PATH.exists():
        _, case5_score_records = get_score_records(
            str(CASE5_RESULTS_PATH),
            CASE5_RESULTS_PATH.stat().st_size,
            CASE5_RESULTS_PATH.stat().st_mtime_ns,
        )
        case5_frame_payload = get_json_payload(
            str(CASE5_FRAME_RESULTS_PATH),
            CASE5_FRAME_RESULTS_PATH.stat().st_size,
            CASE5_FRAME_RESULTS_PATH.stat().st_mtime_ns,
        )
        if CASE5_CLOSEOUT_PATH.exists():
            case5_closeout = get_json_payload(
                str(CASE5_CLOSEOUT_PATH),
                CASE5_CLOSEOUT_PATH.stat().st_size,
                CASE5_CLOSEOUT_PATH.stat().st_mtime_ns,
            )
    case8_score_records = None
    case8_frame_payload = None
    case8_closeout = None
    if CASE8_RESULTS_PATH.exists() and CASE8_FRAME_RESULTS_PATH.exists():
        _, case8_score_records = get_score_records(
            str(CASE8_RESULTS_PATH),
            CASE8_RESULTS_PATH.stat().st_size,
            CASE8_RESULTS_PATH.stat().st_mtime_ns,
        )
        case8_frame_payload = get_json_payload(
            str(CASE8_FRAME_RESULTS_PATH),
            CASE8_FRAME_RESULTS_PATH.stat().st_size,
            CASE8_FRAME_RESULTS_PATH.stat().st_mtime_ns,
        )
        if CASE8_CLOSEOUT_PATH.exists():
            case8_closeout = get_json_payload(
                str(CASE8_CLOSEOUT_PATH),
                CASE8_CLOSEOUT_PATH.stat().st_size,
                CASE8_CLOSEOUT_PATH.stat().st_mtime_ns,
            )
    pending_case_payloads = {}
    for pending_case_id, results_path, frame_path, closeout_path in (
        ("CASE-006", CASE6_RESULTS_PATH, CASE6_FRAME_RESULTS_PATH, CASE6_CLOSEOUT_PATH),
        ("CASE-007", CASE7_RESULTS_PATH, CASE7_FRAME_RESULTS_PATH, CASE7_CLOSEOUT_PATH),
    ):
        score_records = frame_payload = closeout = None
        if results_path.exists() and frame_path.exists():
            _, score_records = get_score_records(
                str(results_path),
                results_path.stat().st_size,
                results_path.stat().st_mtime_ns,
            )
            frame_payload = get_json_payload(
                str(frame_path),
                frame_path.stat().st_size,
                frame_path.stat().st_mtime_ns,
            )
            if closeout_path.exists():
                closeout = get_json_payload(
                    str(closeout_path),
                    closeout_path.stat().st_size,
                    closeout_path.stat().st_mtime_ns,
                )
        pending_case_payloads[pending_case_id] = (
            score_records,
            frame_payload,
            closeout,
        )
    case1_dashboard_records = (
        case1_score_records if t4_readiness["status"] == "READY" else []
    )
    (
        aggregate_dashboard,
        case001_dashboard,
        case002_dashboard,
        case003_dashboard,
        case004_dashboard,
        case005_dashboard,
        case006_dashboard,
        case007_dashboard,
        case008_dashboard,
    ) = st.tabs(
        [
            "תמונה מתכללת",
            "CASE-001 · בית החולים אל־אהלי",
            "CASE-002 · שיירת WCK",
            "CASE-003 · חיסול חסן נסראללה",
            "CASE-004 · המתקפה האיראנית הראשונה",
            "CASE-005 · צווי הביניים בהאג",
            "CASE-006 · חיסול יחיא סינוואר",
            "CASE-007 · תחילת ההפוגה",
            "CASE-008 · מבצע ארנון",
        ]
    )
    with aggregate_dashboard:
        if case1_dashboard_records:
            aggregate_cases = [
                    (
                        "CASE-001",
                        "אל־אהלי",
                        case1_dashboard_records,
                        case1_frame_payload,
                    ),
                    (
                        "CASE-002",
                        "שיירת WCK",
                        case2_score_records,
                        case2_frame_payload,
                    ),
                    (
                        "CASE-003",
                        "חיסול נסראללה",
                        case3_score_records,
                        case3_frame_payload,
                    ),
                    (
                        "CASE-004",
                        "המתקפה האיראנית",
                        case4_score_records,
                        case4_frame_payload,
                    ),
                ]
            if case5_score_records is not None:
                aggregate_cases.append(
                    (
                        "CASE-005",
                        "צווי הביניים בהאג",
                        case5_score_records,
                        case5_frame_payload,
                    )
                )
            for case_id, label in (
                ("CASE-006", "חיסול סינוואר"),
                ("CASE-007", "תחילת ההפוגה"),
            ):
                score_records, frame_payload, _ = pending_case_payloads[case_id]
                if score_records is not None:
                    aggregate_cases.append(
                        (case_id, label, score_records, frame_payload)
                    )
            if case8_score_records is not None:
                aggregate_cases.append(
                    (
                        "CASE-008",
                        "מבצע ארנון",
                        case8_score_records,
                        case8_frame_payload,
                    )
                )
            render_aggregate_dashboard(aggregate_cases)
        else:
            st.warning(
                "התמונה המתכללת ממתינה לסגירה מלאה של CASE-001."
            )

    with case001_dashboard:
        if t4_readiness["status"] != "READY":
            st.warning(
                "דשבורד CASE-001 נעול עד השלמת שבע הכרעות T4. "
                f"נותרו {len(t4_readiness['pending_trajectory_ids'])} מסלולים."
            )
            st.caption(
                "216 רכיבי הכתבה כבר סגורים. לאחר פסק הדין האחרון ב־T4 "
                "הציונים והתרשימים יחושבו דטרמיניסטית ללא קריאת מודל נוספת."
            )
        else:
            render_closed_case_dashboard(
                "CASE-001",
                case1_score_records,
                case1_frame_payload,
                human_decisions=len(current),
            )

    with case002_dashboard:
        render_closed_case_dashboard(
            "CASE-002",
            case2_score_records,
            case2_frame_payload,
            human_decisions=3,
        )

    with case003_dashboard:
        render_closed_case_dashboard(
            "CASE-003",
            case3_score_records,
            case3_frame_payload,
            human_decisions=case3_closeout["human_article_decisions"],
        )

    with case004_dashboard:
        render_closed_case_dashboard(
            "CASE-004",
            case4_score_records,
            case4_frame_payload,
            human_decisions=case4_closeout["human_article_decisions"],
        )

    with case005_dashboard:
        if case5_score_records is not None:
            human_decisions = 2
            if case5_closeout is not None:
                human_decisions = int(
                    case5_closeout.get("human_article_decisions") or 2
                )
            render_closed_case_dashboard(
                "CASE-005",
                case5_score_records,
                case5_frame_payload,
                human_decisions=human_decisions,
            )
        else:
            render_pending_case_dashboard(
                "CASE-005",
                case5_article_summary,
                case5_frame_second_summary,
                case5_review_payload,
            )

    with case008_dashboard:
        if case8_score_records is None:
            st.info("CASE-008 טרם השלים את סגירת תוצאות TF‑2D.")
        else:
            render_closed_case_dashboard(
                "CASE-008",
                case8_score_records,
                case8_frame_payload,
                human_decisions=int(
                    (case8_closeout or {}).get("human_article_decisions") or 0
                ),
            )

    for case_id, dashboard_tab in (
        ("CASE-006", case006_dashboard),
        ("CASE-007", case007_dashboard),
    ):
        with dashboard_tab:
            score_records, frame_payload, closeout = pending_case_payloads[case_id]
            if score_records is None:
                st.info(f"{case_id} ממתין להשלמת T4 ולסגירת תוצאות TF‑2D.")
            else:
                render_closed_case_dashboard(
                    case_id,
                    score_records,
                    frame_payload,
                    human_decisions=int(
                        (closeout or {}).get("human_article_decisions") or 0
                    ),
                )

with policy_tab:
    st.subheader("ספר התקדימים והמדיניות")
    st.write(
        "כל פסק דין יכול להפוך למקרה נקודתי, לתקדים מנחה או למדיניות מחייבת. "
        "ההתאמה לתיק חדש נעשית לפי המדד ותגיות סוגיה גלויות — ללא מודל נוסף וללא שינוי אוטומטי של ציונים."
    )
    policy_decisions = policy_registry["decisions"]
    pending_policy_applications = [
        item for item in policy_decisions
        if item["status"] == "APPROVED_POLICY_REQUIRES_CASE_REVIEW"
    ]
    resolved_policy_applications = policy_registry.get(
        "resolved_applications", []
    )
    metric_policy, metric_a, metric_b, metric_c = st.columns(4)
    metric_policy.metric("כללים שאושרו", len(policy_decisions))
    metric_a.metric("תקדימים פעילים", len(precedents))
    metric_b.metric(
        "מדיניות מחייבת",
        sum(item["precedent_mode"] == "BINDING_POLICY" for item in precedents),
    )
    metric_c.metric("סתירות אפשריות", len(precedent_conflicts))

    st.markdown("### הכרעות המדיניות שאושרו")
    st.dataframe(
        [
            {
                "מזהה": item["packet_item"],
                "בחירה": item["choice"],
                "כלל": item["rule_he"],
                "מצב": (
                    "אושר — נדרשת הכרעת יישום בתיק"
                    if item in pending_policy_applications else "אושר"
                ),
            }
            for item in policy_decisions
        ],
        hide_index=True,
        width="stretch",
    )
    for item in pending_policy_applications:
        st.warning(
            f"{item['packet_item']} · {item['title_he']}: "
            f"{item['implementation_he']}"
        )
    for application in resolved_policy_applications:
        scores = " · ".join(
            f"{component}={score}"
            for component, score in application["resolved_scores"].items()
        )
        st.success(
            f"הכרעת יישום {application['policy_id']} נסגרה בחלופה "
            f"{application['selected_option']}: {scores}. שלושת פסקי הדין "
            "נשמרו כמקרים נקודתיים."
        )
        with st.expander("הצגת נימוק הכרעת היישום"):
            st.write(application["resolution_rationale_he"])
    for application in policy_registry.get("open_applications", []):
        with st.expander(
            f"הכרעת יישום נדרשת · {application['source']} "
            f"{application['component']}",
            expanded=True,
        ):
            st.markdown(f"**{application['question_he']}**")
            st.info(application["governing_rule_he"], icon="🧭")
            st.markdown("##### המוצגים המכריעים")
            for exhibit in application["decisive_exhibits"]:
                st.markdown(
                    f"<div class='evidence' dir='rtl'>{html.escape(exhibit['quote_original'])}</div>",
                    unsafe_allow_html=True,
                )
                st.caption(f"מיקום: {exhibit['location']}")
            st.markdown("##### מצב הידע במועד הפרסום")
            st.dataframe(
                [
                    {
                        "עוגן": item["anchor_id"],
                        "מצב": item["status"],
                        "ביטחון": item["confidence"] or "—",
                        "משמעות": item["meaning_he"],
                    }
                    for item in application["publication_time_record"]
                ],
                hide_index=True,
                width="stretch",
            )
            st.markdown("##### השלכה על רכיבים קשורים")
            st.warning(application["cross_component_rule_he"])
            st.dataframe(
                [
                    {
                        "רכיב": item["component"],
                        "ציון נוכחי": item["current_score"],
                        "ציון ההשגה": item["challenge_score"],
                        "מדוע הוא קשור": item["reason_he"],
                    }
                    for item in application["linked_components"]
                ],
                hide_index=True,
                width="stretch",
            )
            st.markdown("##### החלופות להכרעה")
            for option in application["options"]:
                recommendation = " · המלצת המערכת" if option.get("recommended") else ""
                st.markdown(
                    f"**{option['option_id']} — {option['label_he']} "
                    f"(N2={option['score']}){recommendation}**"
                )
                st.write(option["argument_he"])

    if precedents:
        precedent_rows = []
        for item in precedents:
            precedent_rows.append({
                "מעמד": PRECEDENT_MODE_HE[item["precedent_mode"]],
                "מקור": item["source"],
                "מדד": item["component"],
                "תוצאה": score_label(item["final_score"], item["final_na_reason"]),
                "הכלל שנלמד": item["policy_rule"],
                "תחום תחולה": " · ".join(
                    ISSUE_TAGS_HE.get(tag, tag) for tag in item["issue_tags"]
                ),
                "מועד": item["decided_at"],
            })
        st.dataframe(precedent_rows, hide_index=True, width="stretch")
        policy_warnings = [
            item for item in precedents if item.get("policy_warning_he")
        ]
        if policy_warnings:
            st.markdown("### חריגים מספר החוקים שמחייבים הכרעה")
            for item in policy_warnings:
                st.warning(
                    f"{item['source']} · {item['component']}: "
                    f"{item['policy_warning_he']}"
                )
    else:
        st.info("עדיין אין פסק דין פעיל שניתן להשתמש בו כתקדים.")

    if precedent_conflicts:
        st.markdown("### סתירות שמחייבות הכרעה אנושית")
        for conflict in precedent_conflicts:
            left = conflict["left"]
            right = conflict["right"]
            shared = " · ".join(
                ISSUE_TAGS_HE.get(tag, tag) for tag in conflict["shared_tags"]
            )
            st.warning(
                f"{conflict['component']}: {left['source']} קיבל "
                f"{score_label(left['final_score'], left['final_na_reason'])}, "
                f"ואילו {right['source']} קיבל "
                f"{score_label(right['final_score'], right['final_na_reason'])}. "
                f"סוגיות משותפות: {shared}."
            )

    with st.expander("כיצד המערכת משתמשת בידע"):
        st.markdown(
            """
            1. פסק הדין נשמר יחד עם הכלל שנלמד ותחום התחולה שלו.
            2. בתיק חדש מוצגים רק תקדימים מאותו מדד שיש להם לפחות סוגיה משותפת.
            3. מדיניות מחייבת מוצגת בעדיפות, אך לעולם אינה שומרת פסק דין אוטומטית.
            4. תוצאות סותרות מוצגות כסתירה אפשרית ומוחזרות לשופט.
            5. שינוי פסק דין יוצר גרסה חדשה; התקדים הישן נשאר ביומן אך מפסיק להיות פעיל.
            """
        )

with scores_tab:
    st.subheader("טבלת הניקוד והנימוקים")
    st.write(
        "הטבלה מרכזת כל סעיף שנמדד, את הצעת המודל והסיבה לה, ואת "
        "התוצאה המחקרית הנוכחית. הכרעה אנושית אינה מוחקת את הצעת המודל; "
        "שתיהן נשמרות ביומן המלא לצורך ביקורת ושחזור."
    )
    score_a, score_b, score_c = st.columns(3)
    score_a.metric("סעיפי ניקוד", len(effective_rows))
    score_b.metric(
        "הכרעות מחדר השיפוט",
        sum(row["authority"] == "HUMAN_DECISION" for row in effective_rows),
    )
    score_c.metric("רשומות ביומן המלא", len(score_ledger))
    st.caption(
        "72 סעיפים = שמונה מדדי כתבה בכל אחת מתשע כתבות הכיול: "
        "שלוש עבריות, שלוש אנגליות ושלוש ערביות; 24 ציוני ייחוס אנושיים "
        "קפואים ו־48 סעיפי הריצה המאוחרת. בקורפוס הקפוא נותרו 18 כתבות "
        "ללא ניתוח — שש בכל שפה. "
        "T4 אינו חסר בטעות; הוא נמדד רק על רצף תלת־שלבי של אותו מו״ל."
    )

    score_register = []
    for row in effective_rows:
        if row["human_outcome"] == "DEFER":
            current_result = "נדחה להמשך"
            authority_label = "החלטת אדם — נדחה"
        elif row["status"] == "PENDING":
            current_result = "ממתין לאדם"
            authority_label = "טרם הוכרע"
        else:
            current_result = score_label(
                row["effective_score"], row["na_reason"]
            )
            authority_label = {
                "HUMAN_DECISION": "הכרעת אדם",
                "HUMAN_REFERENCE": "ייחוס אנושי מאושר",
                "MODEL_PROVISIONAL": "מודל — זמני",
            }[row["authority"]]
        score_register.append({
            "מקור": row["source"],
            "תאריך": row["published_at"],
            "שלב": PHASE_HE.get(row["phase"], row["phase"]),
            "מדד": row["component"],
            "סעיף": COMPONENT_LABELS[row["component"]],
            "הצעת המודל": (
                "לא הופעל — ייחוס אנושי קפוא"
                if row["authority"] == "HUMAN_REFERENCE"
                else score_label(row["model_score"], row["model_na_reason"])
            ),
            "ביטחון המודל": (
                "לא רלוונטי"
                if row["authority"] == "HUMAN_REFERENCE"
                else row["model_confidence"] or "לא דורג"
            ),
            "הסיבה להצעת המודל": row["model_rationale"] or "—",
            "תוצאה מחקרית נוכחית": current_result,
            "סמכות": authority_label,
            "הסיבה לתוצאה הנוכחית": (
                row["effective_rationale"]
                or "טרם הוענק ציון מחקרי; נימוק המודל נשמר כהצעה בלבד."
            ),
            "גרסת הכרעה אנושית": (
                str(row["decision_version"]) if row["decision_version"] else ""
            ),
        })
    st.dataframe(score_register, hide_index=True, width="stretch")
    st.download_button(
        "הורדת טבלת המצב הנוכחי כ־CSV",
        data=rows_as_csv(score_register),
        file_name="case001_component_scores_current.csv",
        mime="text/csv",
    )

    with st.expander(f"יומן ניקוד מלא ובלתי־מחיק · {len(score_ledger)} רשומות"):
        ledger_display = [
            {
                "מזהה רשומה": row["record_id"],
                "מקור": row["source_name"],
                "תאריך": row["published_at"],
                "מדד": row["component"],
                "שכבה": (
                    {
                        "MODEL_PROPOSAL": "הצעת מודל",
                        "HUMAN_REFERENCE": "ייחוס אנושי מאושר",
                        "HUMAN_DECISION": "הכרעת אדם",
                    }[row["authority"]]
                ),
                "ציון": score_label(row["score"], row["na_reason"]),
                "סיבה": row["rationale"],
                "מצב": row["status"],
                "גרסה": (
                    str(row["decision_version"])
                    if row["decision_version"] else ""
                ),
                "נוכחית": "כן" if row["is_current"] else "לא",
                "אפקטיבית": "כן" if row["is_effective"] else "לא",
                "שופט": row["decided_by"] or "",
                "מועד רישום": row["recorded_at"] or "פלט מודל קפוא",
            }
            for row in score_ledger
        ]
        st.dataframe(ledger_display, hide_index=True, width="stretch")
        st.download_button(
            "הורדת היומן המלא כ־CSV",
            data=rows_as_csv(score_ledger),
            file_name="case001_component_score_ledger.csv",
            mime="text/csv",
        )

with effective_tab:
    st.subheader("התוצאה שהמערכת משתמשת בה כעת")
    st.caption("רכיבים שלא נותבו נשארים זמניים; אמת־מידה אנושית מאושרת נשארת סמכות אפקטיבית, וסתירה קשה ממתינה לפסק דין. אין עדיין ציון משוקלל סופי.")
    display_rows = []
    for row in effective_rows:
        display_rows.append({
            "מקור": row["source"],
            "תאריך": row["published_at"],
            "מדד": row["component"],
            "ציון אפקטיבי": score_label(row["effective_score"], row["na_reason"]) if row["status"] != "PENDING" else "ממתין לאדם",
            "מעמד": {
                "DECIDED": "החלטה אנושית",
                "HUMAN_REFERENCE": "ייחוס אנושי מאושר",
                "REFERENCE_CONFIRMED": "אמת־מידה אנושית מאושרת",
                "PENDING": "ממתין",
                "PROVISIONAL": "זמני מהמודל",
            }[row["status"]],
        })
    st.dataframe(display_rows, hide_index=True, width="stretch")
    st.download_button(
        "הורדת ההחלטות הנוכחיות כ-JSONL",
        data=decisions_as_jsonl(current.values()),
        file_name="case001_human_decisions_current.jsonl",
        mime="application/x-ndjson",
        disabled=not bool(current),
    )

with journal_tab:
    st.subheader("יומן החלטות בלתי־מחיק")
    st.caption("היומן מציג את כל הגרסאות. החלפה מסמנת גרסה קודמת כהיסטורית ואינה מוחקת אותה.")
    source_by_dilemma = {
        dilemma["dilemma_id"]: dilemma["article"].get("source_name") or dilemma["source_id"]
        for dilemma in material.dilemmas
    }
    all_history = [
        {
            "מועד": item["decided_at"],
            "מקור": source_by_dilemma.get(item["dilemma_id"], item["document_id"]),
            "מדד": item["component"],
            "גרסה": item["decision_version"],
            "הכרעה": OUTCOME_HE[item["outcome"]],
            "ציון": score_label(item["final_score"], item["final_na_reason"]),
            "שופט": item["decided_by"],
            "נוכחית": "כן" if item["is_current"] else "לא",
            "נימוק": item["rationale"],
            "השפעה עתידית": PRECEDENT_MODE_HE.get(
                item.get("precedent_mode"), "תקדים מנחה"
            ),
            "הכלל שנשמר": item.get("policy_rule", ""),
        }
        for item in store.all_history()
    ]
    if all_history:
        all_history.sort(key=lambda item: item["מועד"], reverse=True)
        st.dataframe(all_history, hide_index=True, width="stretch")
    else:
        st.info("היומן עדיין ריק. כל 11 התיקים ממתינים להכרעה ראשונה.")
