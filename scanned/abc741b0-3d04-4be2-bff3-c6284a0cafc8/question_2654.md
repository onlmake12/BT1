# Q2654: Medium storage state transition mismatch in contains_key

## Question
Can an unprivileged attacker enter through a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases and sequence database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size so `contains_key` in `shared/src/types/header_map/mod.rs` observes pre-state and post-state from different views, letting the flow force large storage or lookup amplification with a small number of valid blocks or transactions, violating database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `shared/src/types/header_map/mod.rs::contains_key`
- Entrypoint: a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases
- Attacker controls: database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size
- Exploit idea: force large storage or lookup amplification with a small number of valid blocks or transactions
- Invariant to test: database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
