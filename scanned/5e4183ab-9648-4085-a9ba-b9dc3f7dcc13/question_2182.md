# Q2182: High rpc restart reorg persistence in util

## Question
Can an unprivileged attacker shape block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a light-client protocol caller requesting proofs and filters across reorg boundaries, then force normal restart, reorg, retry, or replay handling so `util` in `rpc/src/util/mod.rs` persists inconsistent state and amplify storage scans or proof generation with small crafted RPC requests, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/util/mod.rs::util`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
