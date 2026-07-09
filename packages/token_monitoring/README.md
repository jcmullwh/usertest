# token_monitoring

Metadata-only token inefficiency monitoring for `usertest` runs.

The package reconciles local Codex `token_count` events, joins them to run
artifacts, and emits causal signals such as wait/poll context resend, broad
source/config reads, retained large output, retry loops, delegation tradeoffs,
raw broad-source delegation leaks, and unsupported provider telemetry gaps.

Delegation reports explicitly classify runs as `no_delegation`,
`delegation_parent_context_tradeoff`, `delegation_parent_context_summary`,
`delegation_raw_broad_source_leak`, or `delegation_without_parent_summary`.
They report parent input tokens separately from combined parent-plus-delegated
total tokens so an increase in total tokens can be distinguished from simple
parent-context waste.

It must not copy raw prompts, source bodies, secrets, or full command output
into derived artifacts.

## Canonical smoke

## Standalone package checkout (recommended first path)

```bash
pdm run smoke
pdm run test
pdm run lint
```

## Monorepo contributor workflow

Use the monorepo scaffold commands from the repository root when validating this
package together with the rest of the workspace.
