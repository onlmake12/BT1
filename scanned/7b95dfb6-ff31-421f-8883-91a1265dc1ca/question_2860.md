# Q2860: Critical storage differential path split in versionbits_active

## Question
Can an unprivileged attacker reach `versionbits_active` in `util/snapshot/src/lib.rs` through two production paths from a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases and make one path accept while the other rejects because of index keys, number-hash mappings, cell status transitions, and restart timing, violating persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/snapshot/src/lib.rs::versionbits_active`
- Entrypoint: a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases
- Attacker controls: index keys, number-hash mappings, cell status transitions, and restart timing
- Exploit idea: force large storage or lookup amplification with a small number of valid blocks or transactions
- Invariant to test: persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
