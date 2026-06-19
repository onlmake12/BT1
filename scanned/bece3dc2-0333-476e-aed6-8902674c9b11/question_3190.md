# Q3190: Critical transaction parser precheck gap in try_from

## Question
Can an unprivileged attacker submit malformed-but-reachable maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies through a tx-pool submitter racing mempool admission against chain reorg or cell status changes so `try_from` in `util/types/src/core/blockchain.rs` performs expensive or unsafe work before validation and bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/blockchain.rs::try_from`
- Entrypoint: a tx-pool submitter racing mempool admission against chain reorg or cell status changes
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
