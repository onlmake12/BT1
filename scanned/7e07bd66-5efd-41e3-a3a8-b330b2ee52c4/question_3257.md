# Q3257: Critical transaction boundary divergence in new_builder

## Question
Can an unprivileged attacker enter through a block relayer including dependency-heavy transactions in an otherwise valid block and use maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies to drive `new_builder` in `util/types/src/core/hardfork/ckb2023.rs` across a boundary where make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating the invariant that capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/hardfork/ckb2023.rs::new_builder`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
