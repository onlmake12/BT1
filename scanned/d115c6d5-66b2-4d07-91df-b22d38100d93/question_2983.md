# Q2983: Critical transaction boundary divergence in get_block_epoch

## Question
Can an unprivileged attacker enter through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values and use input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries to drive `get_block_epoch` in `traits/src/epoch_provider.rs` across a boundary where make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating the invariant that resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `traits/src/epoch_provider.rs::get_block_epoch`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
