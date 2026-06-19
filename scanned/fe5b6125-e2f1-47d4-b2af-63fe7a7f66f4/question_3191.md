# Q3191: Critical transaction resource amplification in fmt

## Question
Can an unprivileged attacker repeatedly send small cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values to make `fmt` in `util/types/src/core/cell.rs` amplify CPU, memory, storage, or bandwidth and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/cell.rs::fmt`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
