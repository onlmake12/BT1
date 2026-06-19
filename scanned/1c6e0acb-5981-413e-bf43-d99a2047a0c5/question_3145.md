# Q3145: Critical transaction differential path split in build_indexer_cell

## Question
Can an unprivileged attacker reach `build_indexer_cell` in `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs` through two production paths from a tx-pool submitter racing mempool admission against chain reorg or cell status changes and make one path accept while the other rejects because of canonical cell status before and after reorg, snapshot lookup results, and dep-group layout, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs::build_indexer_cell`
- Entrypoint: a tx-pool submitter racing mempool admission against chain reorg or cell status changes
- Attacker controls: canonical cell status before and after reorg, snapshot lookup results, and dep-group layout
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
