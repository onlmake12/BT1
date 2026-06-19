# Q3188: Critical transaction batch interaction bug in try_from

## Question
Can an unprivileged attacker batch input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `try_from` in `util/types/src/core/blockchain.rs` handles the first item safely but applies incorrect assumptions to later items and make dependency resolution use a different cell/header than the script-visible authorization path, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/blockchain.rs::try_from`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
