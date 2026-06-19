# Q2151: High rpc canonical encoding ambiguity in get_deployments_info

## Question
Can an unprivileged attacker craft alternate encodings for JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `get_deployments_info` in `rpc/src/module/stats.rs` accepts two representations for one security object and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/module/stats.rs::get_deployments_info`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
