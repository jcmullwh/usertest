from __future__ import annotations

import argparse

from usertest_backlog.commands.atom_actions import _cmd_reports_sync_atom_actions
from usertest_backlog.commands.export_tickets import _cmd_reports_export_tickets
from usertest_backlog.commands.reports import (
    _cmd_reports_analyze,
    _cmd_reports_compile,
    _cmd_reports_intent_snapshot,
    _cmd_reports_window,
)
from usertest_backlog.commands.review_ux import _cmd_reports_review_ux
from usertest_backlog.commands.triage import _cmd_triage_atoms, _cmd_triage_backlog, _cmd_triage_prs
from usertest_backlog.workflows.staged import _cmd_reports_backlog


def dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "reports":
        if args.reports_cmd == "compile":
            return _cmd_reports_compile(args)
        if args.reports_cmd == "analyze":
            return _cmd_reports_analyze(args)
        if args.reports_cmd == "window":
            return _cmd_reports_window(args)
        if args.reports_cmd == "intent-snapshot":
            return _cmd_reports_intent_snapshot(args)
        if args.reports_cmd == "review-ux":
            return _cmd_reports_review_ux(args)
        if args.reports_cmd == "sync-atom-actions":
            return _cmd_reports_sync_atom_actions(args)
        if args.reports_cmd == "export-tickets":
            return _cmd_reports_export_tickets(args)
        if args.reports_cmd == "backlog":
            return _cmd_reports_backlog(args)
        return 2
    if args.cmd == "triage-prs":
        return _cmd_triage_prs(args)
    if args.cmd == "triage-backlog":
        return _cmd_triage_backlog(args)
    if args.cmd == "triage-atoms":
        return _cmd_triage_atoms(args)
    return 2


__all__ = ["dispatch"]
