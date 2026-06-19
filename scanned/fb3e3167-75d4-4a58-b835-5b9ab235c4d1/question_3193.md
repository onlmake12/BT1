# Q3193: Critical transaction restart reorg persistence in is_dead

## Question
Can an unprivileged attacker shape maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values, then force normal restart, reorg, retry, or replay handling so `is_dead` in `util/types/src/core/cell.rs` persists inconsistent state and bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/cell.rs::is_dead`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
