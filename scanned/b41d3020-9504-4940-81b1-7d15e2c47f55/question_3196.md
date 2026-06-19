# Q3196: Critical transaction cache invalidation failure in is_lack_of_capacity

## Question
Can an unprivileged attacker use a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values to alternate valid and invalid input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries so `is_lack_of_capacity` in `util/types/src/core/cell.rs` leaves a cache, index, or status flag stale and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/cell.rs::is_lack_of_capacity`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
