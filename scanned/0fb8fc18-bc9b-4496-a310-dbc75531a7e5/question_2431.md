# Q2431: Low rpc resource amplification in index_tx_pool

## Question
Can an unprivileged attacker repeatedly send small RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs to make `index_tx_pool` in `util/rich-indexer/src/service.rs` amplify CPU, memory, storage, or bandwidth and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/service.rs::index_tx_pool`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
