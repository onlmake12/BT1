# Q3107: Critical transaction boundary divergence in one

## Question
Can an unprivileged attacker enter through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values and use maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies to drive `one` in `util/occupied-capacity/core/src/units.rs` across a boundary where make dependency resolution use a different cell/header than the script-visible authorization path, violating the invariant that transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/occupied-capacity/core/src/units.rs::one`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
