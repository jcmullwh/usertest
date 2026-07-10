from backlog_repo import ticket_export_fingerprint


def test_ticket_export_fingerprint_is_stable() -> None:
    ticket = {
        "title": "Add quickstart examples",
        "problem": "docs/README.md lacks one-command setup",
        "change_surface": {"kinds": ["docs_change"]},
        "suggested_owner": "docs",
    }
    assert ticket_export_fingerprint(ticket) == ticket_export_fingerprint(ticket)


def test_case_fingerprint_ignores_generated_wording_and_owner_drift() -> None:
    first = {
        "case_id": "case:missing-report-lifecycle",
        "plan_revision_id": "plan:missing-report:v1",
        "title": "Centralize lifecycle classification",
        "suggested_owner": "runner_core",
    }
    rewritten = {
        **first,
        "title": "Introduce a shared run lifecycle contract",
        "suggested_owner": "run_artifacts",
    }

    assert ticket_export_fingerprint(first) == ticket_export_fingerprint(rewritten)


def test_case_fingerprint_changes_only_for_explicit_plan_revision() -> None:
    ticket = {
        "case_id": "case:missing-report-lifecycle",
        "plan_revision_id": "plan:missing-report:v1",
    }
    revised = {**ticket, "plan_revision_id": "plan:missing-report:v2"}

    assert ticket_export_fingerprint(ticket) != ticket_export_fingerprint(revised)
