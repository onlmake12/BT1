# Q2202: Low rpc limit off by one in index_tx_pool

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for indexer state freshness, reorg timing, block-filter requests, and proof target positions through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `index_tx_pool` in `util/indexer-sync/src/pool.rs` return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/indexer-sync/src/pool.rs::index_tx_pool`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
