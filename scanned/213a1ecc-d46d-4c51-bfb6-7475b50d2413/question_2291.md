# Q2291: High rpc boundary divergence in new

## Question
Can an unprivileged attacker enter through a light-client protocol caller requesting proofs and filters across reorg boundaries and use block/template parameters, transaction payloads, fee-rate values, and debug/experiment options to drive `new` in `util/jsonrpc-types/src/indexer.rs` across a boundary where amplify storage scans or proof generation with small crafted RPC requests, violating the invariant that proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/indexer.rs::new`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
