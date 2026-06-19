# Q3285: Critical transaction restart reorg persistence in core

## Question
Can an unprivileged attacker shape canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values, then force normal restart, reorg, retry, or replay handling so `core` in `util/types/src/core/mod.rs` persists inconsistent state and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/mod.rs::core`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
