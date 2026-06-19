# Q2394: High rpc differential path split in fmt

## Question
Can an unprivileged attacker reach `fmt` in `util/light-client-protocol-server/src/status.rs` through two production paths from a light-client protocol caller requesting proofs and filters across reorg boundaries and make one path accept while the other rejects because of block/template parameters, transaction payloads, fee-rate values, and debug/experiment options, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/light-client-protocol-server/src/status.rs::fmt`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
