# Q2345: Low rpc resource amplification in deserialize_decimal

## Question
Can an unprivileged attacker repeatedly send small indexer state freshness, reorg timing, block-filter requests, and proof target positions through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values to make `deserialize_decimal` in `util/jsonrpc-types/src/uints.rs` amplify CPU, memory, storage, or bandwidth and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/uints.rs::deserialize_decimal`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
