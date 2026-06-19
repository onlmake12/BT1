# Q2346: High rpc boundary divergence in deserialize_with_redundant_leading_zeros

## Question
Can an unprivileged attacker enter through a local RPC caller invoking public JSON-RPC methods with crafted parameters and use RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence to drive `deserialize_with_redundant_leading_zeros` in `util/jsonrpc-types/src/uints.rs` across a boundary where make RPC/indexer code panic or allocate heavily before validation clamps the request, violating the invariant that RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/uints.rs::deserialize_with_redundant_leading_zeros`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
