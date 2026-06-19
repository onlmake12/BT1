# Q2129: Low rpc cache invalidation failure in local_node_info

## Question
Can an unprivileged attacker use a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs to alternate valid and invalid RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence so `local_node_info` in `rpc/src/module/net.rs` leaves a cache, index, or status flag stale and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/net.rs::local_node_info`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
