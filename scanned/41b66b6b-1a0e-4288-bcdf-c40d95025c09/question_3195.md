# Q3195: Critical transaction boundary divergence in is_dead

## Question
Can an unprivileged attacker enter through a tx-pool submitter racing mempool admission against chain reorg or cell status changes and use canonical cell status before and after reorg, snapshot lookup results, and dep-group layout to drive `is_dead` in `util/types/src/core/cell.rs` across a boundary where make dependency resolution use a different cell/header than the script-visible authorization path, violating the invariant that capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/cell.rs::is_dead`
- Entrypoint: a tx-pool submitter racing mempool admission against chain reorg or cell status changes
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
