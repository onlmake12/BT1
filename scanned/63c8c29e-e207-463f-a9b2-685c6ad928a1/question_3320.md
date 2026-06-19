# Q3320: High transaction canonical encoding ambiguity in with_unknown

## Question
Can an unprivileged attacker craft alternate encodings for canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `with_unknown` in `util/types/src/core/tx_pool.rs` accepts two representations for one security object and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/tx_pool.rs::with_unknown`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
