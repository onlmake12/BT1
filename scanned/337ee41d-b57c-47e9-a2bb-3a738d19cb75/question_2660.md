# Q2660: Critical storage restart reorg persistence in new

## Question
Can an unprivileged attacker shape block order, reorg depth, cell lifetimes, transaction metadata, header maps, and snapshot timing through a node receiving valid-looking blocks that stress snapshots, caches, freezer, and write batches, then force normal restart, reorg, retry, or replay handling so `new` in `shared/src/types/header_map/mod.rs` persists inconsistent state and make persisted state disagree with canonical verification state after restart or rollback, violating state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `shared/src/types/header_map/mod.rs::new`
- Entrypoint: a node receiving valid-looking blocks that stress snapshots, caches, freezer, and write batches
- Attacker controls: block order, reorg depth, cell lifetimes, transaction metadata, header maps, and snapshot timing
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
