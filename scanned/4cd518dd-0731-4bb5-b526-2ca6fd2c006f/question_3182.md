# Q3182: High transaction limit off by one in DepType

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a block relayer including dependency-heavy transactions in an otherwise valid block so `DepType` in `util/types/src/core/blockchain.rs` make dependency resolution use a different cell/header than the script-visible authorization path, violating transactions cannot spend cells without satisfying lock/type script authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/blockchain.rs::DepType`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
