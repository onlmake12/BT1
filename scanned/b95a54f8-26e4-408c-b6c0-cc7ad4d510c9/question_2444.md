# Q2444: Critical storage limit off by one in is_non_empty_db

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for index keys, number-hash mappings, cell status transitions, and restart timing through a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases so `is_non_empty_db` in `db-migration/src/lib.rs` make persisted state disagree with canonical verification state after restart or rollback, violating cell/header/transaction metadata must match the verified chain and never expose stale spendability, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `db-migration/src/lib.rs::is_non_empty_db`
- Entrypoint: a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases
- Attacker controls: index keys, number-hash mappings, cell status transitions, and restart timing
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: cell/header/transaction metadata must match the verified chain and never expose stale spendability
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
