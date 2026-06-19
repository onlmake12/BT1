# Q2987: High transaction batch interaction bug in get_block_header

## Question
Can an unprivileged attacker batch canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `get_block_header` in `traits/src/epoch_provider.rs` handles the first item safely but applies incorrect assumptions to later items and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `traits/src/epoch_provider.rs::get_block_header`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
