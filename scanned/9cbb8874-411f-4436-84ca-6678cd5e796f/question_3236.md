# Q3236: Critical transaction state transition mismatch in fmt

## Question
Can an unprivileged attacker enter through a block relayer including dependency-heavy transactions in an otherwise valid block and sequence cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values so `fmt` in `util/types/src/core/fee_rate.rs` observes pre-state and post-state from different views, letting the flow bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/fee_rate.rs::fmt`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
