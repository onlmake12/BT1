# Q2311: Low rpc cache invalidation failure in NodeAddress

## Question
Can an unprivileged attacker use an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values to alternate valid and invalid block/template parameters, transaction payloads, fee-rate values, and debug/experiment options so `NodeAddress` in `util/jsonrpc-types/src/net.rs` leaves a cache, index, or status flag stale and amplify storage scans or proof generation with small crafted RPC requests, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/net.rs::NodeAddress`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
