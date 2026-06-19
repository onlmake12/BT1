# Q2287: High rpc canonical encoding ambiguity in IndexerPagination

## Question
Can an unprivileged attacker craft alternate encodings for JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `IndexerPagination` in `util/jsonrpc-types/src/indexer.rs` accepts two representations for one security object and amplify storage scans or proof generation with small crafted RPC requests, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/indexer.rs::IndexerPagination`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
