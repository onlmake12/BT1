# Q2171: High rpc replay reorder race in enable_pool

## Question
Can an unprivileged attacker replay, reorder, or delay block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a light-client protocol caller requesting proofs and filters across reorg boundaries so `enable_pool` in `rpc/src/service_builder.rs` takes a stale branch and amplify storage scans or proof generation with small crafted RPC requests, breaking the invariant that proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/service_builder.rs::enable_pool`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
