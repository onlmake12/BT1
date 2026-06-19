# Q2375: Low rpc canonical encoding ambiguity in constant

## Question
Can an unprivileged attacker craft alternate encodings for block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a light-client protocol caller requesting proofs and filters across reorg boundaries so `constant` in `util/light-client-protocol-server/src/constant.rs` accepts two representations for one security object and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/light-client-protocol-server/src/constant.rs::constant`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
