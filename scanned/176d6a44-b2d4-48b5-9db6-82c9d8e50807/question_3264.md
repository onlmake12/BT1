# Q3264: Critical transaction limit off by one in $name_struct

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `$name_struct` in `util/types/src/core/hardfork/helper.rs` create a state transition where capacity or spendability changes without a matching valid authorization, violating transactions cannot spend cells without satisfying lock/type script authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/hardfork/helper.rs::$name_struct`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: create a state transition where capacity or spendability changes without a matching valid authorization
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
