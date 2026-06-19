# Q2985: High transaction replay reorder race in get_block_ext

## Question
Can an unprivileged attacker replay, reorder, or delay canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `get_block_ext` in `traits/src/epoch_provider.rs` takes a stale branch and make dependency resolution use a different cell/header than the script-visible authorization path, breaking the invariant that tx-pool admission and block verification must not diverge for security-relevant validity, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `traits/src/epoch_provider.rs::get_block_ext`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
