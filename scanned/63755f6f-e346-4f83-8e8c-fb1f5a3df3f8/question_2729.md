# Q2729: Critical storage resource amplification in get_tip_header

## Question
Can an unprivileged attacker repeatedly send small block order, reorg depth, cell lifetimes, transaction metadata, header maps, and snapshot timing through a peer-driven chain/reorg sequence that writes adversarial canonical and fork state to make `get_tip_header` in `store/src/store.rs` amplify CPU, memory, storage, or bandwidth and make persisted state disagree with canonical verification state after restart or rollback, violating persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `store/src/store.rs::get_tip_header`
- Entrypoint: a peer-driven chain/reorg sequence that writes adversarial canonical and fork state
- Attacker controls: block order, reorg depth, cell lifetimes, transaction metadata, header maps, and snapshot timing
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
