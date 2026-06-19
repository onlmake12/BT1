# Q3319: High transaction boundary divergence in with_rejected

## Question
Can an unprivileged attacker enter through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values and use maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies to drive `with_rejected` in `util/types/src/core/tx_pool.rs` across a boundary where create a state transition where capacity or spendability changes without a matching valid authorization, violating the invariant that resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/tx_pool.rs::with_rejected`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: create a state transition where capacity or spendability changes without a matching valid authorization
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
