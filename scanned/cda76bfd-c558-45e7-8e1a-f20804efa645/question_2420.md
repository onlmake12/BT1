# Q2420: High rpc state transition mismatch in get_indexer_tip

## Question
Can an unprivileged attacker enter through a local RPC caller invoking public JSON-RPC methods with crafted parameters and sequence RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence so `get_indexer_tip` in `util/rich-indexer/src/indexer_handle/mod.rs` observes pre-state and post-state from different views, letting the flow make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rich-indexer/src/indexer_handle/mod.rs::get_indexer_tip`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
