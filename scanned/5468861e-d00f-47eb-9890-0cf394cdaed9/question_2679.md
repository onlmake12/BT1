# Q2679: Medium storage cache invalidation failure in from_config

## Question
Can an unprivileged attacker use a peer-driven chain/reorg sequence that writes adversarial canonical and fork state to alternate valid and invalid block order, reorg depth, cell lifetimes, transaction metadata, header maps, and snapshot timing so `from_config` in `store/src/cache.rs` leaves a cache, index, or status flag stale and make persisted state disagree with canonical verification state after restart or rollback, violating database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `store/src/cache.rs::from_config`
- Entrypoint: a peer-driven chain/reorg sequence that writes adversarial canonical and fork state
- Attacker controls: block order, reorg depth, cell lifetimes, transaction metadata, header maps, and snapshot timing
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
