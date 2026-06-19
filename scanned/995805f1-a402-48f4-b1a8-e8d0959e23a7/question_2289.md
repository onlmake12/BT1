# Q2289: High rpc cache invalidation failure in IndexerSearchMode

## Question
Can an unprivileged attacker use an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values to alternate valid and invalid block/template parameters, transaction payloads, fee-rate values, and debug/experiment options so `IndexerSearchMode` in `util/jsonrpc-types/src/indexer.rs` leaves a cache, index, or status flag stale and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/indexer.rs::IndexerSearchMode`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
