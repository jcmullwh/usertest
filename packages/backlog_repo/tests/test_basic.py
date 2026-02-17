from backlog_repo import ticket_export_fingerprint


def test_ticket_export_fingerprint_is_stable() -> None:
    ticket = {
        "title": "Add quickstart examples",
        "problem": "docs/README.md lacks one-command setup",
        "change_surface": {"kinds": ["docs_change"]},
        "suggested_owner": "docs",
    }
    assert ticket_export_fingerprint(ticket) == ticket_export_fingerprint(ticket)
