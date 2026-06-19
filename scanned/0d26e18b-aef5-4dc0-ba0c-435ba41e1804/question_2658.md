# Q2658: Medium storage restart reorg persistence in new

## Question
Can an unprivileged attacker shape cache pressure, rollback order, canonical/forked block alternation, and missing extension/filter data through a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases, then force normal restart, reorg, retry, or replay handling so `new` in `shared/src/types/header_map/mod.rs` persists inconsistent state and lose, duplicate, or stale-cache cells/headers across snapshots and write batches, violating persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `shared/src/types/header_map/mod.rs::new`
- Entrypoint: a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases
- Attacker controls: cache pressure, rollback order, canonical/forked block alternation, and missing extension/filter data
- Exploit idea: lose, duplicate, or stale-cache cells/headers across snapshots and write batches
- Invariant to test: persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
