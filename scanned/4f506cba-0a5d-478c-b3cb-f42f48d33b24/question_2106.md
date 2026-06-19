# Q2106: Low rpc boundary divergence in dry_run_transaction

## Question
Can an unprivileged attacker enter through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs and use RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence to drive `dry_run_transaction` in `rpc/src/module/experiment.rs` across a boundary where amplify storage scans or proof generation with small crafted RPC requests, violating the invariant that local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/experiment.rs::dry_run_transaction`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
