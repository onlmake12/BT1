# Q2332: High rpc differential path split in LogEntry

## Question
Can an unprivileged attacker reach `LogEntry` in `util/jsonrpc-types/src/subscription.rs` through two production paths from a light-client protocol caller requesting proofs and filters across reorg boundaries and make one path accept while the other rejects because of JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/subscription.rs::LogEntry`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
