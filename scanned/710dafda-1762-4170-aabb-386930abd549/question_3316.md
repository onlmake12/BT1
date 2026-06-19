# Q3316: High transaction cache invalidation failure in is_allowed_relay

## Question
Can an unprivileged attacker use a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values to alternate valid and invalid maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies so `is_allowed_relay` in `util/types/src/core/tx_pool.rs` leaves a cache, index, or status flag stale and make dependency resolution use a different cell/header than the script-visible authorization path, violating capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/tx_pool.rs::is_allowed_relay`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
