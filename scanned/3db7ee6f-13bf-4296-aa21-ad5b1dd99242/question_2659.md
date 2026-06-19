# Q2659: Critical storage boundary divergence in new

## Question
Can an unprivileged attacker enter through an operator replay/import/restart after attacker-shaped blocks, transactions, or indexes were persisted and use database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size to drive `new` in `shared/src/types/header_map/mod.rs` across a boundary where force large storage or lookup amplification with a small number of valid blocks or transactions, violating the invariant that database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `shared/src/types/header_map/mod.rs::new`
- Entrypoint: an operator replay/import/restart after attacker-shaped blocks, transactions, or indexes were persisted
- Attacker controls: database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size
- Exploit idea: force large storage or lookup amplification with a small number of valid blocks or transactions
- Invariant to test: database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
