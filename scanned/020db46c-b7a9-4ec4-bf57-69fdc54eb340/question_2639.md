# Q2639: Critical storage cross module inconsistency in trace_progress_reset

## Question
Can an unprivileged attacker use a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases to make `trace_progress_reset` in `shared/src/types/header_map/kernel_lru.rs` return a result that downstream modules interpret differently, where make persisted state disagree with canonical verification state after restart or rollback, violating database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `shared/src/types/header_map/kernel_lru.rs::trace_progress_reset`
- Entrypoint: a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases
- Attacker controls: cache pressure, rollback order, canonical/forked block alternation, and missing extension/filter data
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
