# Q2281: High rpc boundary divergence in Serialize

## Question
Can an unprivileged attacker enter through a light-client protocol caller requesting proofs and filters across reorg boundaries and use RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence to drive `Serialize` in `util/jsonrpc-types/src/fixed_bytes.rs` across a boundary where return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating the invariant that RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/fixed_bytes.rs::Serialize`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
