# Q2240: High rpc canonical encoding ambiguity in From

## Question
Can an unprivileged attacker craft alternate encodings for RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `From` in `util/jsonrpc-types/src/alert.rs` accepts two representations for one security object and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/alert.rs::From`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
