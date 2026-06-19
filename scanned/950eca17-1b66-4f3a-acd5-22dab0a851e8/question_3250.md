# Q3250: Critical transaction limit off by one in script_result_changed_at

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `script_result_changed_at` in `util/types/src/core/hardfork/ckb2021.rs` bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/hardfork/ckb2021.rs::script_result_changed_at`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
