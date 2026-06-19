# Q3242: Critical transaction differential path split in CKB2021

## Question
Can an unprivileged attacker reach `CKB2021` in `util/types/src/core/hardfork/ckb2021.rs` through two production paths from a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values and make one path accept while the other rejects because of cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/hardfork/ckb2021.rs::CKB2021`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
