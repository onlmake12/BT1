# Q2154: High rpc cache invalidation failure in default

## Question
Can an unprivileged attacker use a light-client protocol caller requesting proofs and filters across reorg boundaries to alternate valid and invalid block/template parameters, transaction payloads, fee-rate values, and debug/experiment options so `default` in `rpc/src/module/terminal.rs` leaves a cache, index, or status flag stale and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/module/terminal.rs::default`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
