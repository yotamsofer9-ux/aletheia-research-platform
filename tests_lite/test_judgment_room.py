from pathlib import Path

from aletheia_lite.judgment_room import (
    JudgmentStore,
    build_effective_rows,
    build_precedent_records,
    decision_status,
    find_precedent_conflicts,
    find_relevant_precedents,
    load_case_material,
    load_policy_registry,
)


BASE_DIR = Path(__file__).resolve().parents[1]


def test_frozen_inputs_build_exactly_eleven_pending_dilemmas():
    material = load_case_material(BASE_DIR)
    assert len(material.dilemmas) == 11
    assert len({item["dilemma_id"] for item in material.dilemmas}) == 11
    assert all(item["model"]["decision"] == "HUMAN_REVIEW" for item in material.dilemmas)


def test_every_non_hebrew_exhibit_has_a_documented_hebrew_translation():
    material = load_case_material(BASE_DIR)
    translated = [
        item
        for item in material.dilemmas
        if item["language"] != "he" and item.get("decisive_exhibit")
    ]

    assert translated
    for item in translated:
        record = item["evidence_translation"]
        assert record is not None
        assert record["target_language"] == "he"
        assert record["source_text"] == item["decisive_exhibit"]["quote_original"]
        assert record["translation_he"]
        assert record["glossary"]


def test_alaraby_t1_exposes_headline_and_time_qualified_anchors():
    material = load_case_material(BASE_DIR)
    dilemma = next(
        item
        for item in material.dilemmas
        if item["source_id"] == "alaraby-arabic" and item["component"] == "T1"
    )

    assert dilemma["model"]["evidence_span"] is None
    assert dilemma["model"]["anchor_refs"] == []
    assert dilemma["decisive_exhibit"]["location"] == "HEADLINE"
    assert dilemma["evidence_translation"]["translation_he"].startswith("טבח")
    assert {
        item["anchor_id"]: item["role"] for item in dilemma["anchor_review"]
    } == {
        "CASE001-CASUALTY-001": "CONTEXT_ONLY_AVAILABLE",
        "CASE001-ACTOR-001": "PROPOSITION_MATCH_BUT_LATER",
        "CASE001-CAUSE-002": "COMPETING_HYPOTHESIS_BUT_LATER",
    }
    assert {anchor["anchor_id"] for anchor in dilemma["anchors"]} == {
        "CASE001-CASUALTY-001",
        "CASE001-ACTOR-001",
        "CASE001-CAUSE-002",
    }


def test_nyt_t1_exposes_auditable_visual_exhibits_without_local_video_blobs():
    material = load_case_material(BASE_DIR)
    dilemma = next(
        item
        for item in material.dilemmas
        if item["source_id"] == "new-york-times-english"
        and item["component"] == "T1"
    )

    exhibits = dilemma["visual_exhibits"]
    assert [item["exhibit_id"] for item in exhibits] == [
        "CASE001-NYT-VISUAL-001",
        "CASE001-NYT-VISUAL-002",
    ]
    assert all(item["media_type"] == "video" for item in exhibits)
    assert all(item["media_url"].startswith("https://vp.nyt.com/") for item in exhibits)
    assert all(item["poster_url"].startswith("https://static01.nyt.com/") for item in exhibits)
    assert all(item["caption_he"] for item in exhibits)
    assert all(item["proves_he"] and item["does_not_prove_he"] for item in exhibits)
    assert all(
        item["provenance"]["snapshot_hash"] == dilemma["article"]["snapshot_hash"]
        for item in exhibits
    )
    assert all("local_path" not in item for item in exhibits)


def test_accept_challenge_changes_only_effective_layer(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")
    dilemma = next(item for item in material.dilemmas if item["source_id"] == "n12-hebrew-substitute" and item["component"] == "T1")
    original_model_score = dilemma["model"]["score"]
    decision = store.save_decision(
        dilemma, outcome="ACCEPT_CHALLENGE",
        rationale="The publication-time anchor requires the lower score.",
        decided_by="test judge",
    )
    rows = build_effective_rows(material, store.current_decisions())
    effective = next(row for row in rows if row["document_id"] == dilemma["document_id"] and row["component"] == "T1")
    assert original_model_score == 4
    assert dilemma["model"]["score"] == 4
    assert decision["final_score"] == 1
    assert effective["effective_score"] == 1
    assert effective["authority"] == "HUMAN_DECISION"


def test_revised_decision_preserves_history(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")
    dilemma = material.dilemmas[0]
    first = store.save_decision(
        dilemma, outcome="DEFER", rationale="More evidence is needed.", decided_by="test judge"
    )
    second = store.save_decision(
        dilemma, outcome="CUSTOM", rationale="The evidence supports a middle score.",
        decided_by="test judge", custom_score=2,
    )
    history = store.history(dilemma["dilemma_id"])
    assert first["decision_version"] == 1
    assert second["decision_version"] == 2
    assert len(history) == 2
    assert sum(item["is_current"] for item in history) == 1
    assert history[0]["supersedes_decision_id"] == first["decision_id"]
    assert decision_status(history[0]) == "DECIDED"


def test_deferred_and_unresolved_components_have_no_effective_score(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")
    deferred = material.dilemmas[0]
    store.save_decision(
        deferred, outcome="DEFER", rationale="Keep this issue open.", decided_by="test judge"
    )
    routed_rows = [row for row in build_effective_rows(material, store.current_decisions()) if row["status"] == "PENDING"]
    assert len(routed_rows) == 11
    assert all(row["effective_score"] is None for row in routed_rows)


def test_custom_na_is_a_decision_not_a_deferral(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")
    dilemma = material.dilemmas[0]
    decision = store.save_decision(
        dilemma, outcome="CUSTOM", rationale="No proposition-matched anchor exists.",
        decided_by="test judge", custom_is_na=True,
        custom_na_reason="NO_ACTIVE_MATCHED_ANCHOR",
    )
    assert decision["final_score"] is None
    assert decision["final_na_reason"] == "NO_ACTIVE_MATCHED_ANCHOR"
    assert decision_status(decision) == "DECIDED"


def test_score_ledger_preserves_every_model_reason_and_human_version(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")

    assert store.sync_score_ledger(material) == 72
    initial_rows = store.score_ledger_rows()
    assert len(initial_rows) == 72
    model_rows = [
        row for row in initial_rows if row["authority"] == "MODEL_PROPOSAL"
    ]
    reference_rows = [
        row for row in initial_rows if row["authority"] == "HUMAN_REFERENCE"
    ]
    assert len(model_rows) == 48
    assert len(reference_rows) == 24
    assert all(row["rationale"] for row in initial_rows)
    assert sum(row["is_effective"] for row in initial_rows) == 61

    dilemma = material.dilemmas[0]
    first = store.save_decision(
        dilemma,
        outcome="ACCEPT_MODEL",
        rationale="The human accepts the model for this stated reason.",
        decided_by="test judge",
    )
    assert store.sync_score_ledger(material) == 73
    human = next(
        row for row in store.score_ledger_rows()
        if row["record_id"] == f"HUMAN:{first['decision_id']}"
    )
    assert human["rationale"] == "The human accepts the model for this stated reason."
    assert human["is_current"] == 1
    assert human["is_effective"] == 1

    second = store.save_decision(
        dilemma,
        outcome="DEFER",
        rationale="The record is not sufficient yet.",
        decided_by="test judge",
    )
    assert store.sync_score_ledger(material) == 74
    human_rows = [
        row for row in store.score_ledger_rows()
        if row["authority"] == "HUMAN_DECISION"
    ]
    by_id = {row["decision_id"]: row for row in human_rows}
    assert by_id[first["decision_id"]]["is_current"] == 0
    assert by_id[first["decision_id"]]["is_effective"] == 0
    assert by_id[second["decision_id"]]["is_current"] == 1
    assert by_id[second["decision_id"]]["is_effective"] == 0


def test_effective_score_rows_include_the_reason_for_the_current_result(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")
    dilemma = material.dilemmas[0]
    rationale = "This is the explicit reason for the research score."
    store.save_decision(
        dilemma,
        outcome="ACCEPT_MODEL",
        rationale=rationale,
        decided_by="test judge",
    )

    row = next(
        item for item in build_effective_rows(material, store.current_decisions())
        if item["document_id"] == dilemma["document_id"]
        and item["component"] == dilemma["component"]
    )
    assert row["effective_rationale"] == rationale
    assert row["model_rationale"] == dilemma["model"]["rationale"]
    assert row["decision_version"] == 1


def test_saved_judgment_becomes_a_relevant_persuasive_precedent(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")
    source = next(
        item
        for item in material.dilemmas
        if item["source_id"] == "alaraby-arabic" and item["component"] == "N2"
    )
    target = next(
        item
        for item in material.dilemmas
        if item["source_id"] == "n12-hebrew-substitute" and item["component"] == "N2"
    )

    store.save_decision(
        source,
        outcome="ACCEPT_MODEL",
        rationale="The headline closes responsibility.",
        decided_by="test judge",
        policy_rule="A categorical headline can create causal closure despite attribution in the body.",
    )
    precedents = build_precedent_records(material, store.current_decisions())
    matches = find_relevant_precedents(target, precedents)

    assert len(precedents) == 1
    assert precedents[0]["precedent_mode"] == "PERSUASIVE_PRECEDENT"
    assert matches[0]["dilemma_id"] == source["dilemma_id"]
    assert "PREMATURE_CLOSURE" in matches[0]["shared_tags"]


def test_case_only_judgment_is_not_reused_as_precedent(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")
    dilemma = material.dilemmas[0]

    store.save_decision(
        dilemma,
        outcome="ACCEPT_MODEL",
        rationale="This decision is unique to the article.",
        decided_by="test judge",
        precedent_mode="CASE_ONLY",
    )

    assert build_precedent_records(material, store.current_decisions()) == []


def test_inconsistent_similar_precedents_are_flagged_not_resolved(tmp_path):
    material = load_case_material(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")
    alaraby = next(
        item
        for item in material.dilemmas
        if item["source_id"] == "alaraby-arabic" and item["component"] == "N2"
    )
    n12 = next(
        item
        for item in material.dilemmas
        if item["source_id"] == "n12-hebrew-substitute" and item["component"] == "N2"
    )
    store.save_decision(
        alaraby,
        outcome="ACCEPT_MODEL",
        rationale="Use the model score.",
        decided_by="test judge",
    )
    store.save_decision(
        n12,
        outcome="ACCEPT_MODEL",
        rationale="Use the model score.",
        decided_by="test judge",
    )

    conflicts = find_precedent_conflicts(
        build_precedent_records(material, store.current_decisions())
    )

    assert len(conflicts) == 1
    assert conflicts[0]["component"] == "N2"
    assert "PREMATURE_CLOSURE" in conflicts[0]["shared_tags"]


def test_approved_policy_registry_preserves_all_five_human_choices():
    registry = load_policy_registry(BASE_DIR)

    assert registry["status"] == "APPROVED_AND_RECONCILED"
    assert [item["packet_item"] for item in registry["decisions"]] == [
        "D1", "D2", "D3", "D4", "D5"
    ]
    assert all(item["choice"] == "A" for item in registry["decisions"])
    assert all(item["status"] == "APPROVED" for item in registry["decisions"])
    assert registry["open_applications"] == []
    assert len(registry["resolved_applications"]) == 1
    application = registry["resolved_applications"][0]
    assert application["component"] == "N2"
    assert [
        option["option_id"] for option in application["options"]
        if option.get("recommended")
    ] == ["A"]
    assert application["options"][0]["score"] == 3
    assert {item["component"] for item in application["linked_components"]} == {
        "T1", "T3"
    }
    assert application["options"][0]["linked_action"] == (
        "REOPEN_T1_T3_FOR_COHERENT_ADOPTS_TRIPLET"
    )
    assert application["selected_option"] == "C"
    assert application["resolved_scores"] == {"T1": 4, "T3": 4, "N2": 0}


def test_policy_layer_scopes_precedents_and_resolves_all_conflicts(tmp_path):
    material = load_case_material(BASE_DIR)
    registry = load_policy_registry(BASE_DIR)
    store = JudgmentStore(tmp_path / "judgments.sqlite3")

    def dilemma(source_id, component):
        return next(
            item for item in material.dilemmas
            if item["source_id"] == source_id and item["component"] == component
        )

    verdicts = [
        (dilemma("new-york-times-english", "T1"), "ACCEPT_CHALLENGE", None),
        (dilemma("alaraby-arabic", "T1"), "ACCEPT_CHALLENGE", None),
        (dilemma("n12-hebrew-substitute", "T3"), "ACCEPT_MODEL", None),
        (dilemma("alaraby-arabic", "T3"), "ACCEPT_MODEL", None),
        (dilemma("n12-hebrew-substitute", "N2"), "CUSTOM", 0),
        (dilemma("alaraby-arabic", "N2"), "ACCEPT_MODEL", None),
        (dilemma("alaraby-arabic", "N1"), "ACCEPT_MODEL", None),
        (dilemma("alaraby-arabic", "N3"), "ACCEPT_MODEL", None),
    ]
    for item, outcome, custom_score in verdicts:
        store.save_decision(
            item,
            outcome=outcome,
            custom_score=custom_score,
            rationale="Policy regression fixture.",
            decided_by="test judge",
        )

    precedents = build_precedent_records(
        material, store.current_decisions(), registry
    )
    precedent_ids = {item["dilemma_id"] for item in precedents}
    assert dilemma("alaraby-arabic", "N1")["dilemma_id"] not in precedent_ids
    assert dilemma("alaraby-arabic", "N3")["dilemma_id"] not in precedent_ids
    assert dilemma("n12-hebrew-substitute", "T1")["dilemma_id"] not in precedent_ids
    assert dilemma("n12-hebrew-substitute", "T3")["dilemma_id"] not in precedent_ids
    assert dilemma("n12-hebrew-substitute", "N2")["dilemma_id"] not in precedent_ids

    conflicts = find_precedent_conflicts(precedents, registry)
    assert conflicts == []
