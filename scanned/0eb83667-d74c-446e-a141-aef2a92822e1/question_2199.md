# Q2199: High rpc differential path split in rollback

## Question
Can an unprivileged attacker reach `rollback` in `util/indexer-sync/src/lib.rs` through two production paths from a local RPC caller invoking public JSON-RPC methods with crafted parameters and make one path accept while the other rejects because of indexer state freshness, reorg timing, block-filter requests, and proof target positions, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/indexer-sync/src/lib.rs::rollback`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
