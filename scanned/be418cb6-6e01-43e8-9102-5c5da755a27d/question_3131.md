# Q3131: Critical transaction limit off by one in RewardCalculator

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `RewardCalculator` in `util/reward-calculator/src/lib.rs` make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/reward-calculator/src/lib.rs::RewardCalculator`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
