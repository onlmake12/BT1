# Q3184: Critical transaction state transition mismatch in DepType

## Question
Can an unprivileged attacker enter through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values and sequence maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies so `DepType` in `util/types/src/core/blockchain.rs` observes pre-state and post-state from different views, letting the flow make dependency resolution use a different cell/header than the script-visible authorization path, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/blockchain.rs::DepType`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
