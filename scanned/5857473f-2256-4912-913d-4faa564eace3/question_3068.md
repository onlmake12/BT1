# Q3068: High transaction batch interaction bug in From

## Question
Can an unprivileged attacker batch cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `From` in `util/jsonrpc-types/src/cell.rs` handles the first item safely but applies incorrect assumptions to later items and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating transactions cannot spend cells without satisfying lock/type script authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/jsonrpc-types/src/cell.rs::From`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
