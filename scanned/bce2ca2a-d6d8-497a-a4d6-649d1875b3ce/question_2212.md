# Q2212: Low rpc differential path split in get_live_cells_by_type_script

## Question
Can an unprivileged attacker reach `get_live_cells_by_type_script` in `util/indexer/src/indexer.rs` through two production paths from a local RPC caller invoking public JSON-RPC methods with crafted parameters and make one path accept while the other rejects because of JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/indexer/src/indexer.rs::get_live_cells_by_type_script`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
