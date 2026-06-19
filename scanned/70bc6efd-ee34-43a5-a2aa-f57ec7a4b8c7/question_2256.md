# Q2256: Low rpc boundary divergence in serialize

## Question
Can an unprivileged attacker enter through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and use indexer state freshness, reorg timing, block-filter requests, and proof target positions to drive `serialize` in `util/jsonrpc-types/src/bytes.rs` across a boundary where make RPC/indexer code panic or allocate heavily before validation clamps the request, violating the invariant that local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/bytes.rs::serialize`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
