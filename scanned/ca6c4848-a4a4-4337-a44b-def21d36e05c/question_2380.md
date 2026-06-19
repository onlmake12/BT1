# Q2380: Low rpc cache invalidation failure in LightClientProtocol

## Question
Can an unprivileged attacker use a local RPC caller invoking public JSON-RPC methods with crafted parameters to alternate valid and invalid indexer state freshness, reorg timing, block-filter requests, and proof target positions so `LightClientProtocol` in `util/light-client-protocol-server/src/lib.rs` leaves a cache, index, or status flag stale and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/light-client-protocol-server/src/lib.rs::LightClientProtocol`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
