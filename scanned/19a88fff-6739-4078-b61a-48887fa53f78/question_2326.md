# Q2326: Low rpc cache invalidation failure in Deserialize

## Question
Can an unprivileged attacker use a light-client protocol caller requesting proofs and filters across reorg boundaries to alternate valid and invalid JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions so `Deserialize` in `util/jsonrpc-types/src/proposal_short_id.rs` leaves a cache, index, or status flag stale and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/proposal_short_id.rs::Deserialize`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
