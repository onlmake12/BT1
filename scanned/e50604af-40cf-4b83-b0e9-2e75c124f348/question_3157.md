# Q3157: High transaction limit off by one in get_cells_capacity

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a tx-pool submitter racing mempool admission against chain reorg or cell status changes so `get_cells_capacity` in `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs` create a state transition where capacity or spendability changes without a matching valid authorization, violating capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs::get_cells_capacity`
- Entrypoint: a tx-pool submitter racing mempool admission against chain reorg or cell status changes
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: create a state transition where capacity or spendability changes without a matching valid authorization
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
