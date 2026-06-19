# Q3249: High transaction boundary divergence in script_result_changed_at

## Question
Can an unprivileged attacker enter through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values and use cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values to drive `script_result_changed_at` in `util/types/src/core/hardfork/ckb2021.rs` across a boundary where make dependency resolution use a different cell/header than the script-visible authorization path, violating the invariant that capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/hardfork/ckb2021.rs::script_result_changed_at`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
