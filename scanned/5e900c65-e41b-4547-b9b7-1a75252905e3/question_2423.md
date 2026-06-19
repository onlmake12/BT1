# Q2423: Low rpc parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs so `new` in `util/rich-indexer/src/indexer_handle/mod.rs` performs expensive or unsafe work before validation and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer_handle/mod.rs::new`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
