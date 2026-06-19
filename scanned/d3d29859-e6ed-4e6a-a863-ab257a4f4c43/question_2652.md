# Q2652: High storage canonical encoding ambiguity in contains_key

## Question
Can an unprivileged attacker craft alternate encodings for index keys, number-hash mappings, cell status transitions, and restart timing through a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases so `contains_key` in `shared/src/types/header_map/mod.rs` accepts two representations for one security object and make persisted state disagree with canonical verification state after restart or rollback, violating state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `shared/src/types/header_map/mod.rs::contains_key`
- Entrypoint: a sync peer causing repeated rollback, truncation, migration, or state lookup edge cases
- Attacker controls: index keys, number-hash mappings, cell status transitions, and restart timing
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
