# Q3043: High transaction resource amplification in From

## Question
Can an unprivileged attacker repeatedly send small cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values to make `From` in `util/dao/utils/src/error.rs` amplify CPU, memory, storage, or bandwidth and bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/dao/utils/src/error.rs::From`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
