# Q2301: Low rpc batch interaction bug in schema_name

## Question
Can an unprivileged attacker batch RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `schema_name` in `util/jsonrpc-types/src/json_schema.rs` handles the first item safely but applies incorrect assumptions to later items and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/json_schema.rs::schema_name`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
