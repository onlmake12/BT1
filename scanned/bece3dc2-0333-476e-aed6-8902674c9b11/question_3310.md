# Q3310: Critical transaction parser precheck gap in unset_dead

## Question
Can an unprivileged attacker submit malformed-but-reachable cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `unset_dead` in `util/types/src/core/transaction_meta.rs` performs expensive or unsafe work before validation and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/transaction_meta.rs::unset_dead`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
