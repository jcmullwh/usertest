You are triaging onboarding and first-run friction.

From the atom list, output up to {{MAX_TICKETS_PER_MINER}} tickets that improve discoverability, setup, and first successful usage.

Rules:
- Ground every ticket in evidence_atom_ids.
- Bias toward high-impact quick wins.
- Include explicit success_criteria that can be verified.
- Return JSON only (no markdown).

Schema:
[
  {
    "title": "...",
    "problem": "...",
    "user_impact": "...",
    "severity": "blocker|high|medium|low",
    "confidence": 0.0,
    "evidence_atom_ids": ["..."],
    "proposed_fix": "...",
    "investigation_steps": ["..."],
    "success_criteria": ["..."],
    "suggested_owner": "docs|runner_core|sandbox_runner|agent_adapters|unknown"
  }
]

Atoms:
{{ATOMS_JSON}}
