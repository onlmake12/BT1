# Q2112: Low rpc replay reorder race in get_indexer_tip

## Question
Can an unprivileged attacker replay, reorder, or delay RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `get_indexer_tip` in `rpc/src/module/indexer.rs` takes a stale branch and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, breaking the invariant that RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/indexer.rs::get_indexer_tip`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
