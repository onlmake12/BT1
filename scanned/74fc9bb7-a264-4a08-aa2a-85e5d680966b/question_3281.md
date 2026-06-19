# Q3281: Critical transaction boundary divergence in core

## Question
Can an unprivileged attacker enter through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values and use cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values to drive `core` in `util/types/src/core/mod.rs` across a boundary where bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating the invariant that capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/mod.rs::core`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
