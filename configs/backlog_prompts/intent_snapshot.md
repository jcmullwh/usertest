You are generating a "repo intent snapshot" for the maintainers of this repository.

Goal:
- Produce a stable machine snapshot of the repo’s intent and user journeys, grounded ONLY in the provided inputs.
- Do not invent new commands or workflows that are not present in the inputs.
- Prefer specificity over generality.

Output requirements:
- Output MUST be a single valid JSON object and nothing else (no markdown, no commentary).

Return JSON in this schema:
{
  "intent_summary": "1-2 short paragraphs describing what the repo is for and how it should evolve.",
  "user_flows": [
    {
      "name": "Short flow name",
      "steps": ["Step 1", "Step 2"],
      "related_commands": ["usertest ..."]
    }
  ],
  "command_surface_notes": "Short notes about command surface philosophy and what to avoid.",
  "confidence": 0.0
}

Human-owned intent (configs/repo_intent.md):
{{REPO_INTENT_MD}}

Repo README excerpt:
{{README_MD}}

Docs index:
{{DOCS_INDEX_JSON}}

Current CLI commands (machine-extracted):
{{COMMANDS_JSON}}
