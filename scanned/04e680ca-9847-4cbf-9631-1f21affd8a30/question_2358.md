# Q2358: Low rpc restart reorg persistence in new

## Question
Can an unprivileged attacker shape block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values, then force normal restart, reorg, retry, or replay handling so `new` in `util/light-client-protocol-server/src/components/get_last_state.rs` persists inconsistent state and amplify storage scans or proof generation with small crafted RPC requests, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/light-client-protocol-server/src/components/get_last_state.rs::new`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
