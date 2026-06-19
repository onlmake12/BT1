# Q2353: High rpc parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `new` in `util/light-client-protocol-server/src/components/get_blocks_proof.rs` performs expensive or unsafe work before validation and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/light-client-protocol-server/src/components/get_blocks_proof.rs::new`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
