# Q2642: High storage batch interaction bug in contains_key

## Question
Can an unprivileged attacker batch cache pressure, rollback order, canonical/forked block alternation, and missing extension/filter data through a node receiving valid-looking blocks that stress snapshots, caches, freezer, and write batches so `contains_key` in `shared/src/types/header_map/memory.rs` handles the first item safely but applies incorrect assumptions to later items and force large storage or lookup amplification with a small number of valid blocks or transactions, violating state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `shared/src/types/header_map/memory.rs::contains_key`
- Entrypoint: a node receiving valid-looking blocks that stress snapshots, caches, freezer, and write batches
- Attacker controls: cache pressure, rollback order, canonical/forked block alternation, and missing extension/filter data
- Exploit idea: force large storage or lookup amplification with a small number of valid blocks or transactions
- Invariant to test: state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
