# Q2579: Medium storage differential path split in new

## Question
Can an unprivileged attacker reach `new` in `shared/src/chain_services_builder.rs` through two production paths from a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases and make one path accept while the other rejects because of block order, reorg depth, cell lifetimes, transaction metadata, header maps, and snapshot timing, violating persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `shared/src/chain_services_builder.rs::new`
- Entrypoint: a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases
- Attacker controls: block order, reorg depth, cell lifetimes, transaction metadata, header maps, and snapshot timing
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
