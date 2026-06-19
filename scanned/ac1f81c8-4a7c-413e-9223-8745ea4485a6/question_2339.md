# Q2339: High rpc boundary divergence in NetworkInfo

## Question
Can an unprivileged attacker enter through a light-client protocol caller requesting proofs and filters across reorg boundaries and use block/template parameters, transaction payloads, fee-rate values, and debug/experiment options to drive `NetworkInfo` in `util/jsonrpc-types/src/terminal.rs` across a boundary where return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating the invariant that proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/terminal.rs::NetworkInfo`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
