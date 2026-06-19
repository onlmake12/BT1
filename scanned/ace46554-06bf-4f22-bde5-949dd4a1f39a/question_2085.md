# Q2085: High rpc differential path split in rpc_method_is_deprecated

## Question
Can an unprivileged attacker reach `rpc_method_is_deprecated` in `rpc/src/error.rs` through two production paths from a local RPC caller invoking public JSON-RPC methods with crafted parameters and make one path accept while the other rejects because of JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/error.rs::rpc_method_is_deprecated`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
