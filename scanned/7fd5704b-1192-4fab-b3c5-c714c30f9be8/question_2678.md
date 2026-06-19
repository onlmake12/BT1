# Q2678: High storage limit off by one in default

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for index keys, number-hash mappings, cell status transitions, and restart timing through a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases so `default` in `store/src/cache.rs` lose, duplicate, or stale-cache cells/headers across snapshots and write batches, violating state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `store/src/cache.rs::default`
- Entrypoint: a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases
- Attacker controls: index keys, number-hash mappings, cell status transitions, and restart timing
- Exploit idea: lose, duplicate, or stale-cache cells/headers across snapshots and write batches
- Invariant to test: state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
