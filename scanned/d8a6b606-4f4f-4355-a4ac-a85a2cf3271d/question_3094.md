# Q3094: High transaction boundary divergence in lib

## Question
Can an unprivileged attacker enter through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values and use cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values to drive `lib` in `util/occupied-capacity/core/src/lib.rs` across a boundary where make dependency resolution use a different cell/header than the script-visible authorization path, violating the invariant that resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/occupied-capacity/core/src/lib.rs::lib`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
