# Q3286: High transaction batch interaction bug in core

## Question
Can an unprivileged attacker batch maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies through a block relayer including dependency-heavy transactions in an otherwise valid block so `core` in `util/types/src/core/mod.rs` handles the first item safely but applies incorrect assumptions to later items and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating transactions cannot spend cells without satisfying lock/type script authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/mod.rs::core`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
