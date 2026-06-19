# Q3216: Critical transaction cache invalidation failure in last_block_hash_in_previous_epoch

## Question
Can an unprivileged attacker use a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values to alternate valid and invalid canonical cell status before and after reorg, snapshot lookup results, and dep-group layout so `last_block_hash_in_previous_epoch` in `util/types/src/core/extras.rs` leaves a cache, index, or status flag stale and make dependency resolution use a different cell/header than the script-visible authorization path, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/extras.rs::last_block_hash_in_previous_epoch`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
