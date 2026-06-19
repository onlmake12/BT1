# Q2086: High rpc differential path split in rpc_method_is_deprecated

## Question
Can an unprivileged attacker reach `rpc_method_is_deprecated` in `rpc/src/error.rs` through two production paths from a local RPC caller invoking public JSON-RPC methods with crafted parameters and make one path accept while the other rejects because of indexer state freshness, reorg timing, block-filter requests, and proof target positions, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/error.rs::rpc_method_is_deprecated`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
