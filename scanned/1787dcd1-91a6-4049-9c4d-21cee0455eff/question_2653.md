# Q2653: Medium storage state transition mismatch in contains_key

## Question
Can an unprivileged attacker enter through a peer-driven chain/reorg sequence that writes adversarial canonical and fork state and sequence index keys, number-hash mappings, cell status transitions, and restart timing so `contains_key` in `shared/src/types/header_map/mod.rs` observes pre-state and post-state from different views, letting the flow trigger database/freezer/migration panic or unrecoverable state from attacker-shaped accepted data, violating cell/header/transaction metadata must match the verified chain and never expose stale spendability, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `shared/src/types/header_map/mod.rs::contains_key`
- Entrypoint: a peer-driven chain/reorg sequence that writes adversarial canonical and fork state
- Attacker controls: index keys, number-hash mappings, cell status transitions, and restart timing
- Exploit idea: trigger database/freezer/migration panic or unrecoverable state from attacker-shaped accepted data
- Invariant to test: cell/header/transaction metadata must match the verified chain and never expose stale spendability
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
