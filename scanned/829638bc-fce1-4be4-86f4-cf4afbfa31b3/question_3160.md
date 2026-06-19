# Q3160: High transaction limit off by one in get_cells_capacity

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for canonical cell status before and after reorg, snapshot lookup results, and dep-group layout through a block relayer including dependency-heavy transactions in an otherwise valid block so `get_cells_capacity` in `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs` bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating transactions cannot spend cells without satisfying lock/type script authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs::get_cells_capacity`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: transactions cannot spend cells without satisfying lock/type script authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
