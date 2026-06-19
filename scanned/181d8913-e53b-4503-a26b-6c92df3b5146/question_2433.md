# Q2433: Low rpc canonical encoding ambiguity in spawn_poll

## Question
Can an unprivileged attacker craft alternate encodings for indexer state freshness, reorg timing, block-filter requests, and proof target positions through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs so `spawn_poll` in `util/rich-indexer/src/service.rs` accepts two representations for one security object and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/service.rs::spawn_poll`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
