# Q3283: High transaction limit off by one in core

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `core` in `util/types/src/core/mod.rs` make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating transactions cannot spend cells without satisfying lock/type script authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/mod.rs::core`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
