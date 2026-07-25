# Example usage (PowerShell)

$env:PYTHONPATH="apps/usertest/src;packages/runner_core/src;packages/agent_adapters/src;packages/normalized_events/src;packages/reporter/src;packages/sandbox_runner/src"

# Representative validation
python -m usertest.cli run --repo-root . --repo I:\path\to\target-repo --agent codex --policy write --persona-id representative_workflow_evaluator --mission-id verify_install_to_result

# Preflight probe
python -m usertest.cli run --repo-root . --repo I:\path\to\target-repo --agent codex --policy inspect --persona-id quickstart_sprinter --mission-id first_output_smoke

# Batch run
python -m usertest.cli batch --repo-root . --targets examples/targets.yaml --agent codex --policy safe
