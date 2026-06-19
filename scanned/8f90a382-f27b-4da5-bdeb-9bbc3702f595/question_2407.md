# Q2407: Low rpc boundary divergence in set_init_tip

## Question
Can an unprivileged attacker enter through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs and use block/template parameters, transaction payloads, fee-rate values, and debug/experiment options to drive `set_init_tip` in `util/rich-indexer/src/indexer/mod.rs` across a boundary where make RPC/indexer code panic or allocate heavily before validation clamps the request, violating the invariant that proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer/mod.rs::set_init_tip`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
