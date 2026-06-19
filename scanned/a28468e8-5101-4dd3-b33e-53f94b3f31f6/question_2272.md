# Q2272: Low rpc cache invalidation failure in from

## Question
Can an unprivileged attacker use a light-client protocol caller requesting proofs and filters across reorg boundaries to alternate valid and invalid JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions so `from` in `util/jsonrpc-types/src/fee_estimator.rs` leaves a cache, index, or status flag stale and amplify storage scans or proof generation with small crafted RPC requests, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/fee_estimator.rs::from`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
