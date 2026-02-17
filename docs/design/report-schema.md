# Report schema (MVP)

The final report is JSON validated against a mission-selected schema from `configs/report_schemas/`, and the exact schema used is snapshotted into each run directory as `report.schema.json`.

## Philosophy

- Keep it stable and small.
- Prefer evidence pointers (files/commands/URLs) over free-form claims.
- Treat conclusions as hypotheses with bounded confidence.
