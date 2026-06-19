# Q2631: Medium storage canonical encoding ambiguity in contains_key

## Question
Can an unprivileged attacker craft alternate encodings for index keys, number-hash mappings, cell status transitions, and restart timing through an operator replay/import/restart after attacker-shaped blocks, transactions, or indexes were persisted so `contains_key` in `shared/src/types/header_map/kernel_lru.rs` accepts two representations for one security object and trigger database/freezer/migration panic or unrecoverable state from attacker-shaped accepted data, violating database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `shared/src/types/header_map/kernel_lru.rs::contains_key`
- Entrypoint: an operator replay/import/restart after attacker-shaped blocks, transactions, or indexes were persisted
- Attacker controls: index keys, number-hash mappings, cell status transitions, and restart timing
- Exploit idea: trigger database/freezer/migration panic or unrecoverable state from attacker-shaped accepted data
- Invariant to test: database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
