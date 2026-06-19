# Q2082: High rpc limit off by one in ckb_internal_error

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `ckb_internal_error` in `rpc/src/error.rs` cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/error.rs::ckb_internal_error`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
