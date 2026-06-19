# Q2284: High rpc state transition mismatch in from

## Question
Can an unprivileged attacker enter through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and sequence JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions so `from` in `util/jsonrpc-types/src/fixed_bytes.rs` observes pre-state and post-state from different views, letting the flow amplify storage scans or proof generation with small crafted RPC requests, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/fixed_bytes.rs::from`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
