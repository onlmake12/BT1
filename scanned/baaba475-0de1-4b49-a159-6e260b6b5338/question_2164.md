# Q2164: Low rpc differential path split in start_server

## Question
Can an unprivileged attacker reach `start_server` in `rpc/src/server.rs` through two production paths from a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs and make one path accept while the other rejects because of block/template parameters, transaction payloads, fee-rate values, and debug/experiment options, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/server.rs::start_server`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
