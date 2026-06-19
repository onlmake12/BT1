# Q2270: High rpc restart reorg persistence in from

## Question
Can an unprivileged attacker shape JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a local RPC caller invoking public JSON-RPC methods with crafted parameters, then force normal restart, reorg, retry, or replay handling so `from` in `util/jsonrpc-types/src/fee_estimator.rs` persists inconsistent state and amplify storage scans or proof generation with small crafted RPC requests, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/fee_estimator.rs::from`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
