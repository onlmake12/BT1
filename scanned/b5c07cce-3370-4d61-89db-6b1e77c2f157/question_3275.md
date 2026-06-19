# Q3275: Critical transaction state transition mismatch in new_dev

## Question
Can an unprivileged attacker enter through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values and sequence maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies so `new_dev` in `util/types/src/core/hardfork/mod.rs` observes pre-state and post-state from different views, letting the flow make dependency resolution use a different cell/header than the script-visible authorization path, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/hardfork/mod.rs::new_dev`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
