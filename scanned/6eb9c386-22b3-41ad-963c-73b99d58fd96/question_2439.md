# Q2439: High rpc boundary divergence in fmt

## Question
Can an unprivileged attacker enter through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and use block/template parameters, transaction payloads, fee-rate values, and debug/experiment options to drive `fmt` in `util/rich-indexer/src/store.rs` across a boundary where amplify storage scans or proof generation with small crafted RPC requests, violating the invariant that local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rich-indexer/src/store.rs::fmt`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
