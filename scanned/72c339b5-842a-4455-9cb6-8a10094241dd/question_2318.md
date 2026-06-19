# Q2318: Low rpc canonical encoding ambiguity in TxPoolEntries

## Question
Can an unprivileged attacker craft alternate encodings for RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs so `TxPoolEntries` in `util/jsonrpc-types/src/pool.rs` accepts two representations for one security object and amplify storage scans or proof generation with small crafted RPC requests, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/pool.rs::TxPoolEntries`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
