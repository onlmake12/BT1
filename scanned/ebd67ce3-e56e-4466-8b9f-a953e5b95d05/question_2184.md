# Q2184: High rpc differential path split in CustomFilters

## Question
Can an unprivileged attacker reach `CustomFilters` in `util/indexer-sync/src/custom_filters.rs` through two production paths from an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and make one path accept while the other rejects because of indexer state freshness, reorg timing, block-filter requests, and proof target positions, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/indexer-sync/src/custom_filters.rs::CustomFilters`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
