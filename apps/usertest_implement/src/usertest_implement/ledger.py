from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from backlog_repo import (
    bind_outcome_verification_amendment,
    extract_outcome_markdown,
    reconcile_outcome_records,
    reconcile_terminal_outcome_stale_blockers,
    transition_outcome_record,
    upsert_outcome_markdown,
    validate_outcome_record,
)

_LOCK_TIMEOUT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.1
_LOCK_STALE_SECONDS = 300.0


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ledger(path: Path) -> dict[str, Any]:
    """Load the implementation ledger and fail on corrupt durable state."""

    if not path.exists():
        return {"schema_version": 1, "updated_at": None, "actions": {}}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Unable to read implementation ledger: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Implementation ledger must be a mapping: {path}")
    if raw.get("schema_version") != 1:
        raise ValueError(f"Unsupported implementation ledger schema: {path}")
    actions = raw.get("actions")
    if not isinstance(actions, dict):
        raise ValueError(f"Implementation ledger actions must be a mapping: {path}")
    actions_dict: dict[str, Any] = {}
    for fingerprint, entry in actions.items():
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError(f"Implementation ledger contains an invalid fingerprint: {path}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"Implementation ledger entry must be a mapping: {fingerprint!r} in {path}"
            )
        entry_copy = dict(entry)
        entry_fingerprint = entry_copy.get("fingerprint")
        if entry_fingerprint is not None and entry_fingerprint != fingerprint:
            raise ValueError(
                "Implementation ledger entry fingerprint mismatch: "
                f"key={fingerprint!r} entry={entry_fingerprint!r}"
            )
        outcome = entry_copy.get("outcome")
        if outcome is not None:
            if not isinstance(outcome, dict):
                raise ValueError(f"Ledger outcome must be a mapping: {fingerprint!r}")
            entry_copy["outcome"] = validate_outcome_record(outcome)
        actions_dict[fingerprint] = entry_copy
    updated_at_raw = raw.get("updated_at")
    updated_at = (
        updated_at_raw
        if isinstance(updated_at_raw, str) and updated_at_raw.strip()
        else None
    )
    return {
        "schema_version": 1,
        "updated_at": updated_at,
        "actions": actions_dict,
    }


def update_ledger_doc(
    doc: dict[str, Any],
    *,
    fingerprint: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Apply validated updates to one fingerprint entry."""

    now = _utc_now_z()
    actions_raw = doc.get("actions")
    actions: dict[str, Any] = actions_raw if isinstance(actions_raw, dict) else {}

    entry_raw = actions.get(fingerprint)
    entry: dict[str, Any] = entry_raw if isinstance(entry_raw, dict) else {}
    changed = False

    if entry.get("fingerprint") != fingerprint:
        changed = True
    entry["fingerprint"] = fingerprint
    for key, value in updates.items():
        if value is None:
            continue
        if key == "outcome":
            if not isinstance(value, dict):
                raise ValueError("Ledger outcome update must be a mapping")
            value = validate_outcome_record(value)
            current_outcome = entry.get("outcome")
            if isinstance(current_outcome, dict):
                value = reconcile_outcome_records(current_outcome, value)
        if entry.get(key) != value:
            changed = True
            entry[key] = value

    if changed:
        entry["updated_at"] = now

    actions[fingerprint] = entry
    doc["schema_version"] = 1
    if changed:
        doc["updated_at"] = now
    doc["actions"] = actions
    return doc


def write_ledger(path: Path, doc: dict[str, Any]) -> None:
    """Persist the implementation ledger as deterministic YAML."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )


def _acquire_lock(path: Path) -> Path:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as err:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = None
            if age is not None and age > _LOCK_STALE_SECONDS:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() - started > _LOCK_TIMEOUT_SECONDS:
                raise TimeoutError(
                    f"Timed out waiting for ledger lock: {lock_path}"
                ) from err
            time.sleep(_LOCK_POLL_SECONDS)
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\n")
                handle.write(f"started_at={_utc_now_z()}\n")
        except Exception:
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise
        return lock_path


def update_ledger_file(path: Path, *, fingerprint: str, updates: dict[str, Any]) -> dict[str, Any]:
    lock_path = _acquire_lock(path)
    try:
        doc = load_ledger(path)
        updated = update_ledger_doc(doc, fingerprint=fingerprint, updates=updates)
        write_ledger(path, updated)
        return updated
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def transition_outcome_files(
    *,
    ledger_path: Path,
    ticket_path: Path,
    fingerprint: str,
    state: str,
    recorded_at: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Advance the ticket and ledger outcome as one rollback-safe transaction.

    Both durable stores must already contain the same validated outcome. Staged
    replacements are validated before either live file changes. If the second
    replacement fails, the first is restored while the ledger lock is held.
    """

    lock_path = _acquire_lock(ledger_path)
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    staged_ticket = ticket_path.with_name(f".{ticket_path.name}.{token}.tmp")
    staged_ledger = ledger_path.with_name(f".{ledger_path.name}.{token}.tmp")
    rollback_ledger = ledger_path.with_name(f".{ledger_path.name}.{token}.rollback")
    try:
        ticket_bytes = ticket_path.read_bytes()
        try:
            ticket_markdown = ticket_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Outcome ticket is not valid UTF-8: {ticket_path}") from exc
        ticket_outcome = extract_outcome_markdown(ticket_markdown)
        if ticket_outcome is None:
            raise ValueError(f"Ticket has no durable outcome record: {ticket_path}")
        from usertest_implement.tickets import parse_ticket_markdown_metadata

        ticket_metadata = parse_ticket_markdown_metadata(ticket_markdown)
        metadata_fingerprint = ticket_metadata.get("fingerprint")
        if metadata_fingerprint != fingerprint:
            raise ValueError(
                "Outcome ticket fingerprint mismatch: "
                f"expected={fingerprint!r} observed={metadata_fingerprint!r}"
            )
        expected_case_id = ticket_metadata.get("case_id") or f"legacy-case:{fingerprint}"
        expected_plan_revision_id = (
            ticket_metadata.get("plan_revision_id") or f"legacy-plan:{fingerprint}"
        )
        if ticket_outcome.get("case_id") != expected_case_id:
            raise ValueError(
                "Outcome ticket case identity mismatch: "
                f"metadata={expected_case_id!r} outcome={ticket_outcome.get('case_id')!r}"
            )
        if ticket_outcome.get("plan_revision_id") != expected_plan_revision_id:
            raise ValueError(
                "Outcome ticket plan identity mismatch: "
                f"metadata={expected_plan_revision_id!r} "
                f"outcome={ticket_outcome.get('plan_revision_id')!r}"
            )

        ledger_existed = ledger_path.exists()
        ledger_bytes = ledger_path.read_bytes() if ledger_existed else b""
        ledger_doc = load_ledger(ledger_path)
        actions = ledger_doc.get("actions")
        entry = actions.get(fingerprint) if isinstance(actions, dict) else None
        ledger_outcome = entry.get("outcome") if isinstance(entry, dict) else None
        if not isinstance(ledger_outcome, dict):
            raise ValueError(
                f"Ledger has no durable outcome for fingerprint {fingerprint!r}"
            )
        ledger_outcome = validate_outcome_record(ledger_outcome)
        if ledger_outcome != ticket_outcome:
            raise ValueError(
                "Outcome stores disagree; refusing a non-atomic transition: "
                f"fingerprint={fingerprint!r}"
            )

        transitioned = transition_outcome_record(
            ticket_outcome,
            state=state,
            recorded_at=recorded_at,
            updates=updates,
        )
        updated_markdown = upsert_outcome_markdown(ticket_markdown, transitioned)
        updated_ledger = update_ledger_doc(
            ledger_doc,
            fingerprint=fingerprint,
            updates={
                "last_outcome_state": transitioned["state"],
                "outcome": transitioned,
            },
        )

        staged_ticket.write_text(updated_markdown, encoding="utf-8")
        staged_ledger.parent.mkdir(parents=True, exist_ok=True)
        staged_ledger.write_text(
            yaml.safe_dump(updated_ledger, sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )

        staged_outcome = extract_outcome_markdown(staged_ticket.read_text(encoding="utf-8"))
        if staged_outcome != transitioned:
            raise OSError(f"Staged ticket outcome verification failed: {staged_ticket}")
        staged_doc = load_ledger(staged_ledger)
        staged_actions = staged_doc.get("actions")
        staged_entry = staged_actions.get(fingerprint) if isinstance(staged_actions, dict) else None
        if not isinstance(staged_entry, dict) or staged_entry.get("outcome") != transitioned:
            raise OSError(f"Staged ledger outcome verification failed: {staged_ledger}")

        ledger_replaced = False
        try:
            os.replace(staged_ledger, ledger_path)
            ledger_replaced = True
            os.replace(staged_ticket, ticket_path)
        except Exception:
            if ledger_replaced:
                try:
                    if ledger_existed:
                        rollback_ledger.write_bytes(ledger_bytes)
                        os.replace(rollback_ledger, ledger_path)
                    else:
                        ledger_path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    raise RuntimeError(
                        "Outcome transaction failed and ledger rollback also failed: "
                        f"{ledger_path}"
                    ) from rollback_exc
            raise
        return transitioned
    finally:
        for temporary in (staged_ticket, staged_ledger, rollback_ledger):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except OSError:
            pass


def bind_outcome_verification_amendment_files(
    *,
    ledger_path: Path,
    ticket_path: Path,
    fingerprint: str,
    verification_commit: str,
    verification_pr_url: str,
    recorded_at: str,
) -> dict[str, Any]:
    """Atomically bind one write-once verification descendant in ticket and ledger."""

    lock_path = _acquire_lock(ledger_path)
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    staged_ticket = ticket_path.with_name(f".{ticket_path.name}.{token}.tmp")
    staged_ledger = ledger_path.with_name(f".{ledger_path.name}.{token}.tmp")
    rollback_ledger = ledger_path.with_name(f".{ledger_path.name}.{token}.rollback")
    try:
        ticket_bytes = ticket_path.read_bytes()
        try:
            ticket_markdown = ticket_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Outcome ticket is not valid UTF-8: {ticket_path}") from exc
        ticket_outcome = extract_outcome_markdown(ticket_markdown)
        if ticket_outcome is None:
            raise ValueError(f"Ticket has no durable outcome record: {ticket_path}")
        from usertest_implement.tickets import parse_ticket_markdown_metadata

        ticket_metadata = parse_ticket_markdown_metadata(ticket_markdown)
        if ticket_metadata.get("fingerprint") != fingerprint:
            raise ValueError(
                "Outcome ticket fingerprint mismatch: "
                f"expected={fingerprint!r} "
                f"observed={ticket_metadata.get('fingerprint')!r}"
            )
        expected_case_id = ticket_metadata.get("case_id") or f"legacy-case:{fingerprint}"
        expected_plan_revision_id = (
            ticket_metadata.get("plan_revision_id") or f"legacy-plan:{fingerprint}"
        )
        if ticket_outcome.get("case_id") != expected_case_id:
            raise ValueError(
                "Outcome ticket case identity mismatch: "
                f"metadata={expected_case_id!r} "
                f"outcome={ticket_outcome.get('case_id')!r}"
            )
        if ticket_outcome.get("plan_revision_id") != expected_plan_revision_id:
            raise ValueError(
                "Outcome ticket plan identity mismatch: "
                f"metadata={expected_plan_revision_id!r} "
                f"outcome={ticket_outcome.get('plan_revision_id')!r}"
            )

        ledger_existed = ledger_path.exists()
        ledger_bytes = ledger_path.read_bytes() if ledger_existed else b""
        ledger_doc = load_ledger(ledger_path)
        actions = ledger_doc.get("actions")
        entry = actions.get(fingerprint) if isinstance(actions, dict) else None
        ledger_outcome = entry.get("outcome") if isinstance(entry, dict) else None
        if not isinstance(ledger_outcome, dict):
            raise ValueError(
                f"Ledger has no durable outcome for fingerprint {fingerprint!r}"
            )
        ledger_outcome = validate_outcome_record(ledger_outcome)
        if ledger_outcome != ticket_outcome:
            raise ValueError(
                "Outcome stores disagree; refusing a non-atomic amendment: "
                f"fingerprint={fingerprint!r}"
            )

        amended = bind_outcome_verification_amendment(
            ticket_outcome,
            verification_commit=verification_commit,
            verification_pr_url=verification_pr_url,
            recorded_at=recorded_at,
        )
        if amended == ticket_outcome:
            return amended
        updated_markdown = upsert_outcome_markdown(ticket_markdown, amended)
        updated_at = _utc_now_z()
        updated_actions = dict(actions)
        updated_entry = dict(entry)
        updated_entry["outcome"] = amended
        updated_entry["updated_at"] = updated_at
        updated_actions[fingerprint] = updated_entry
        updated_ledger = {
            **ledger_doc,
            "schema_version": 1,
            "updated_at": updated_at,
            "actions": updated_actions,
        }

        staged_ticket.write_text(updated_markdown, encoding="utf-8")
        staged_ledger.parent.mkdir(parents=True, exist_ok=True)
        staged_ledger.write_text(
            yaml.safe_dump(updated_ledger, sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )
        if extract_outcome_markdown(staged_ticket.read_text(encoding="utf-8")) != amended:
            raise OSError(f"Staged ticket amendment verification failed: {staged_ticket}")
        staged_doc = load_ledger(staged_ledger)
        staged_actions = staged_doc.get("actions")
        staged_entry = (
            staged_actions.get(fingerprint) if isinstance(staged_actions, dict) else None
        )
        if not isinstance(staged_entry, dict) or staged_entry.get("outcome") != amended:
            raise OSError(f"Staged ledger amendment verification failed: {staged_ledger}")

        ledger_replaced = False
        try:
            os.replace(staged_ledger, ledger_path)
            ledger_replaced = True
            os.replace(staged_ticket, ticket_path)
        except Exception:
            if ledger_replaced:
                try:
                    if ledger_existed:
                        rollback_ledger.write_bytes(ledger_bytes)
                        os.replace(rollback_ledger, ledger_path)
                    else:
                        ledger_path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    raise RuntimeError(
                        "Outcome amendment failed and ledger rollback also failed: "
                        f"{ledger_path}"
                    ) from rollback_exc
            raise
        return amended
    finally:
        for temporary in (staged_ticket, staged_ledger, rollback_ledger):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except OSError:
            pass


def reconcile_terminal_outcome_stale_blockers_files(
    *,
    ledger_path: Path,
    ticket_path: Path,
    fingerprint: str,
) -> dict[str, Any]:
    """Atomically remove only stale runner blockers from a terminal outcome."""

    lock_path = _acquire_lock(ledger_path)
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    staged_ticket = ticket_path.with_name(f".{ticket_path.name}.{token}.tmp")
    staged_ledger = ledger_path.with_name(f".{ledger_path.name}.{token}.tmp")
    rollback_ledger = ledger_path.with_name(f".{ledger_path.name}.{token}.rollback")
    try:
        ticket_bytes = ticket_path.read_bytes()
        try:
            ticket_markdown = ticket_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Outcome ticket is not valid UTF-8: {ticket_path}") from exc
        ticket_outcome = extract_outcome_markdown(ticket_markdown)
        if ticket_outcome is None:
            raise ValueError(f"Ticket has no durable outcome record: {ticket_path}")
        from usertest_implement.tickets import parse_ticket_markdown_metadata

        metadata = parse_ticket_markdown_metadata(ticket_markdown)
        if metadata.get("fingerprint") != fingerprint:
            raise ValueError(
                "Outcome ticket fingerprint mismatch: "
                f"expected={fingerprint!r} observed={metadata.get('fingerprint')!r}"
            )
        expected_case_id = metadata.get("case_id") or f"legacy-case:{fingerprint}"
        expected_plan_revision_id = (
            metadata.get("plan_revision_id") or f"legacy-plan:{fingerprint}"
        )
        if ticket_outcome.get("case_id") != expected_case_id:
            raise ValueError("Outcome ticket case identity mismatch")
        if ticket_outcome.get("plan_revision_id") != expected_plan_revision_id:
            raise ValueError("Outcome ticket plan identity mismatch")

        ledger_existed = ledger_path.exists()
        ledger_bytes = ledger_path.read_bytes() if ledger_existed else b""
        ledger_doc = load_ledger(ledger_path)
        actions = ledger_doc.get("actions")
        entry = actions.get(fingerprint) if isinstance(actions, dict) else None
        ledger_outcome = entry.get("outcome") if isinstance(entry, dict) else None
        if not isinstance(ledger_outcome, dict):
            raise ValueError(
                f"Ledger has no durable outcome for fingerprint {fingerprint!r}"
            )
        ledger_outcome = validate_outcome_record(ledger_outcome)
        if ledger_outcome != ticket_outcome:
            raise ValueError(
                "Outcome stores disagree; refusing a non-atomic stale-blocker "
                f"reconciliation: fingerprint={fingerprint!r}"
            )

        reconciled = reconcile_terminal_outcome_stale_blockers(ticket_outcome)
        if reconciled == ticket_outcome:
            return reconciled
        if {
            key: value
            for key, value in reconciled.items()
            if key != "remaining_risks"
        } != {
            key: value
            for key, value in ticket_outcome.items()
            if key != "remaining_risks"
        }:
            raise RuntimeError(
                "Terminal stale-blocker reconciliation changed protected outcome fields"
            )

        updated_markdown = upsert_outcome_markdown(ticket_markdown, reconciled)
        updated_at = _utc_now_z()
        updated_actions = dict(actions)
        updated_entry = dict(entry)
        updated_entry["outcome"] = reconciled
        updated_entry["updated_at"] = updated_at
        updated_actions[fingerprint] = updated_entry
        updated_ledger = {
            **ledger_doc,
            "schema_version": 1,
            "updated_at": updated_at,
            "actions": updated_actions,
        }

        staged_ticket.write_bytes(updated_markdown.encode("utf-8"))
        staged_ledger.write_text(
            yaml.safe_dump(updated_ledger, sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )
        if extract_outcome_markdown(
            staged_ticket.read_bytes().decode("utf-8")
        ) != reconciled:
            raise OSError(f"Staged ticket reconciliation verification failed: {staged_ticket}")
        staged_doc = load_ledger(staged_ledger)
        staged_actions = staged_doc.get("actions")
        staged_entry = (
            staged_actions.get(fingerprint) if isinstance(staged_actions, dict) else None
        )
        if not isinstance(staged_entry, dict) or staged_entry.get("outcome") != reconciled:
            raise OSError(f"Staged ledger reconciliation verification failed: {staged_ledger}")

        ledger_replaced = False
        try:
            os.replace(staged_ledger, ledger_path)
            ledger_replaced = True
            os.replace(staged_ticket, ticket_path)
        except Exception:
            if ledger_replaced:
                try:
                    if ledger_existed:
                        rollback_ledger.write_bytes(ledger_bytes)
                        os.replace(rollback_ledger, ledger_path)
                    else:
                        ledger_path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    raise RuntimeError(
                        "Outcome reconciliation failed and ledger rollback also failed: "
                        f"{ledger_path}"
                    ) from rollback_exc
            raise
        return reconciled
    finally:
        for temporary in (staged_ticket, staged_ledger, rollback_ledger):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except OSError:
            pass
