# Q3137: High transaction canonical encoding ambiguity in get_proposal_ids_by_hash

## Question
Can an unprivileged attacker craft alternate encodings for input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `get_proposal_ids_by_hash` in `util/reward-calculator/src/lib.rs` accepts two representations for one security object and make dependency resolution use a different cell/header than the script-visible authorization path, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/reward-calculator/src/lib.rs::get_proposal_ids_by_hash`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
