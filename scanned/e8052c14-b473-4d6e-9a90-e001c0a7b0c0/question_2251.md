# Q2251: High rpc differential path split in committed

## Question
Can an unprivileged attacker reach `committed` in `util/jsonrpc-types/src/blockchain.rs` through two production paths from an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and make one path accept while the other rejects because of RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/blockchain.rs::committed`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
