# Q3315: Critical transaction cache invalidation failure in is_allowed_relay

## Question
Can an unprivileged attacker use a tx-pool submitter racing mempool admission against chain reorg or cell status changes to alternate valid and invalid input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries so `is_allowed_relay` in `util/types/src/core/tx_pool.rs` leaves a cache, index, or status flag stale and make dependency resolution use a different cell/header than the script-visible authorization path, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/tx_pool.rs::is_allowed_relay`
- Entrypoint: a tx-pool submitter racing mempool admission against chain reorg or cell status changes
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
