# Q3318: Critical transaction cache invalidation failure in is_unknown

## Question
Can an unprivileged attacker use a block relayer including dependency-heavy transactions in an otherwise valid block to alternate valid and invalid maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies so `is_unknown` in `util/types/src/core/tx_pool.rs` leaves a cache, index, or status flag stale and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/tx_pool.rs::is_unknown`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
