# Q3279: Critical transaction cache invalidation failure in new_mirana

## Question
Can an unprivileged attacker use a tx-pool submitter racing mempool admission against chain reorg or cell status changes to alternate valid and invalid maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies so `new_mirana` in `util/types/src/core/hardfork/mod.rs` leaves a cache, index, or status flag stale and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/hardfork/mod.rs::new_mirana`
- Entrypoint: a tx-pool submitter racing mempool admission against chain reorg or cell status changes
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
