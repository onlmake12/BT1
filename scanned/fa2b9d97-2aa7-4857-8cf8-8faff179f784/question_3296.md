# Q3296: High transaction limit off by one in BlockIssuance

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `BlockIssuance` in `util/types/src/core/reward.rs` make dependency resolution use a different cell/header than the script-visible authorization path, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/reward.rs::BlockIssuance`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
