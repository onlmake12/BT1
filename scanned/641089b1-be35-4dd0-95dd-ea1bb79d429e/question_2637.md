# Q2637: Medium storage parser precheck gap in tick_primary_insert

## Question
Can an unprivileged attacker submit malformed-but-reachable database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size through an operator replay/import/restart after attacker-shaped blocks, transactions, or indexes were persisted so `tick_primary_insert` in `shared/src/types/header_map/kernel_lru.rs` performs expensive or unsafe work before validation and trigger database/freezer/migration panic or unrecoverable state from attacker-shaped accepted data, violating cell/header/transaction metadata must match the verified chain and never expose stale spendability, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `shared/src/types/header_map/kernel_lru.rs::tick_primary_insert`
- Entrypoint: an operator replay/import/restart after attacker-shaped blocks, transactions, or indexes were persisted
- Attacker controls: database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size
- Exploit idea: trigger database/freezer/migration panic or unrecoverable state from attacker-shaped accepted data
- Invariant to test: cell/header/transaction metadata must match the verified chain and never expose stale spendability
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
