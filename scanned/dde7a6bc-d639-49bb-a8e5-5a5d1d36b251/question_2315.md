# Q2315: High rpc cache invalidation failure in From

## Question
Can an unprivileged attacker use a light-client protocol caller requesting proofs and filters across reorg boundaries to alternate valid and invalid JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions so `From` in `util/jsonrpc-types/src/pool.rs` leaves a cache, index, or status flag stale and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/pool.rs::From`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
