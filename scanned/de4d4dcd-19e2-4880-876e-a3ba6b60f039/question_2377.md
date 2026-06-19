# Q2377: High rpc differential path split in constant

## Question
Can an unprivileged attacker reach `constant` in `util/light-client-protocol-server/src/constant.rs` through two production paths from a local RPC caller invoking public JSON-RPC methods with crafted parameters and make one path accept while the other rejects because of RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/light-client-protocol-server/src/constant.rs::constant`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
