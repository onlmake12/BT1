# Q2314: High rpc differential path split in EntryCompleted

## Question
Can an unprivileged attacker reach `EntryCompleted` in `util/jsonrpc-types/src/pool.rs` through two production paths from an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and make one path accept while the other rejects because of indexer state freshness, reorg timing, block-filter requests, and proof target positions, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/pool.rs::EntryCompleted`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
