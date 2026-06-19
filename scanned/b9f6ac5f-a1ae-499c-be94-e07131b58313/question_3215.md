# Q3215: Critical transaction parser precheck gap in last_block_hash_in_previous_epoch

## Question
Can an unprivileged attacker submit malformed-but-reachable cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `last_block_hash_in_previous_epoch` in `util/types/src/core/extras.rs` performs expensive or unsafe work before validation and bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/extras.rs::last_block_hash_in_previous_epoch`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
