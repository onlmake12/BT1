# Q2271: Low rpc cache invalidation failure in from

## Question
Can an unprivileged attacker use a local RPC caller invoking public JSON-RPC methods with crafted parameters to alternate valid and invalid RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence so `from` in `util/jsonrpc-types/src/fee_estimator.rs` leaves a cache, index, or status flag stale and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/fee_estimator.rs::from`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
