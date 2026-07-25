# Delegation A/B validation

- Evidence strength: `partial_missing_authoritative_token_join`
- Tradeoff conclusion: `token_tradeoff_unattributable`
- Combined input delta: `None`
- Combined total token delta: `None`
- Parent input peak delta: `None`

## Arms

| Arm | Runs | Successes | Avg parent peak | Avg combined input | Broad-read signals | Large-context signals |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `delegation_disabled` | 1 | 1 | None | None | 0 | 0 |
| `delegation_enabled` | 1 | 1 | None | None | 0 | 0 |

## Evaluation

At least one arm lacks authoritative token counters.

## Next actions

- Run more same-ticket disabled/enabled maintenance pairs before making delegation more aggressive.
