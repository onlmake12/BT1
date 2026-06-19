# Q2880: High transaction cross module inconsistency in tx_info

## Question
Can an unprivileged attacker use a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values to make `tx_info` in `script/src/syscalls/load_cell.rs` return a result that downstream modules interpret differently, where make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/load_cell.rs::tx_info`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
