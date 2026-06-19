# Q3040: High transaction resource amplification in withdrawed_interests

## Question
Can an unprivileged attacker repeatedly send small canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values to make `withdrawed_interests` in `util/dao/src/lib.rs` amplify CPU, memory, storage, or bandwidth and bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/dao/src/lib.rs::withdrawed_interests`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
