# Q2298: High rpc limit off by one in json_schema

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for indexer state freshness, reorg timing, block-filter requests, and proof target positions through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `json_schema` in `util/jsonrpc-types/src/json_schema.rs` return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/json_schema.rs::json_schema`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
