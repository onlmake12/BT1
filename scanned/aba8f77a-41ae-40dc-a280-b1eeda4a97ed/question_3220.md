# Q3220: Critical transaction cache invalidation failure in set_remainder_reward

## Question
Can an unprivileged attacker use a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values to alternate valid and invalid cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values so `set_remainder_reward` in `util/types/src/core/extras.rs` leaves a cache, index, or status flag stale and bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/extras.rs::set_remainder_reward`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
