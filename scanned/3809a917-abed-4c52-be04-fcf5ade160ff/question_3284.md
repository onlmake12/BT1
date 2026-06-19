# Q3284: High transaction replay reorder race in core

## Question
Can an unprivileged attacker replay, reorder, or delay maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies through a tx-pool submitter racing mempool admission against chain reorg or cell status changes so `core` in `util/types/src/core/mod.rs` takes a stale branch and make dependency resolution use a different cell/header than the script-visible authorization path, breaking the invariant that tx-pool admission and block verification must not diverge for security-relevant validity, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/mod.rs::core`
- Entrypoint: a tx-pool submitter racing mempool admission against chain reorg or cell status changes
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
