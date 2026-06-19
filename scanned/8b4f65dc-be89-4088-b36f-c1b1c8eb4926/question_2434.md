# Q2434: Low rpc differential path split in build_url_for_postgres

## Question
Can an unprivileged attacker reach `build_url_for_postgres` in `util/rich-indexer/src/store.rs` through two production paths from an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and make one path accept while the other rejects because of RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/store.rs::build_url_for_postgres`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
