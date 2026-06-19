# Q2899: Critical transaction parser precheck gap in detach_block_cell

## Question
Can an unprivileged attacker submit malformed-but-reachable canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a block relayer including dependency-heavy transactions in an otherwise valid block so `detach_block_cell` in `store/src/cell.rs` performs expensive or unsafe work before validation and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `store/src/cell.rs::detach_block_cell`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
