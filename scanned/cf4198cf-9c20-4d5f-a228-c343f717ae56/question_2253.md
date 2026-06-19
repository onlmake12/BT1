# Q2253: High rpc boundary divergence in new_rfc0043

## Question
Can an unprivileged attacker enter through a light-client protocol caller requesting proofs and filters across reorg boundaries and use RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence to drive `new_rfc0043` in `util/jsonrpc-types/src/blockchain.rs` across a boundary where cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating the invariant that local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/blockchain.rs::new_rfc0043`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
