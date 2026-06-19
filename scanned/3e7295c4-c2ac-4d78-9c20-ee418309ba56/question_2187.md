# Q2187: Low rpc limit off by one in is_cell_filter_match

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for indexer state freshness, reorg timing, block-filter requests, and proof target positions through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `is_cell_filter_match` in `util/indexer-sync/src/custom_filters.rs` return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/indexer-sync/src/custom_filters.rs::is_cell_filter_match`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
