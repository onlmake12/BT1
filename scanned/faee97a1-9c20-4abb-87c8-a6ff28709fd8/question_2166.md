# Q2166: High rpc cache invalidation failure in start_tcp_server

## Question
Can an unprivileged attacker use a local RPC caller invoking public JSON-RPC methods with crafted parameters to alternate valid and invalid RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence so `start_tcp_server` in `rpc/src/server.rs` leaves a cache, index, or status flag stale and amplify storage scans or proof generation with small crafted RPC requests, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/server.rs::start_tcp_server`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
