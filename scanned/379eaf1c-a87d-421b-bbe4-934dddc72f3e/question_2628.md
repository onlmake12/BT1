# Q2628: High storage cross module inconsistency in remove_no_return

## Question
Can an unprivileged attacker use an operator replay/import/restart after attacker-shaped blocks, transactions, or indexes were persisted to make `remove_no_return` in `shared/src/types/header_map/backend_sled.rs` return a result that downstream modules interpret differently, where make persisted state disagree with canonical verification state after restart or rollback, violating state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `shared/src/types/header_map/backend_sled.rs::remove_no_return`
- Entrypoint: an operator replay/import/restart after attacker-shaped blocks, transactions, or indexes were persisted
- Attacker controls: database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
