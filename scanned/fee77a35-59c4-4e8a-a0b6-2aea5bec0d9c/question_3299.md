# Q3299: Critical transaction boundary divergence in from

## Question
Can an unprivileged attacker enter through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values and use input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries to drive `from` in `util/types/src/core/reward.rs` across a boundary where make dependency resolution use a different cell/header than the script-visible authorization path, violating the invariant that transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/reward.rs::from`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
