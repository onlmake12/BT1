# Q2736: Critical storage limit off by one in insert_cells

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size through a node receiving valid-looking blocks that stress snapshots, caches, freezer, and write batches so `insert_cells` in `store/src/write_batch.rs` trigger database/freezer/migration panic or unrecoverable state from attacker-shaped accepted data, violating persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `store/src/write_batch.rs::insert_cells`
- Entrypoint: a node receiving valid-looking blocks that stress snapshots, caches, freezer, and write batches
- Attacker controls: database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size
- Exploit idea: trigger database/freezer/migration panic or unrecoverable state from attacker-shaped accepted data
- Invariant to test: persisted CKB state must remain canonical, recoverable, and internally consistent across reorg/restart
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
