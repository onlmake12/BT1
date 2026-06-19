# Q2408: High rpc cross module inconsistency in tip

## Question
Can an unprivileged attacker use an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values to make `tip` in `util/rich-indexer/src/indexer/mod.rs` return a result that downstream modules interpret differently, where return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rich-indexer/src/indexer/mod.rs::tip`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
