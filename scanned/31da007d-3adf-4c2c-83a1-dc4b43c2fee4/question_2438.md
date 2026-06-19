# Q2438: High rpc parser precheck gap in fetch_one_by_query_as

## Question
Can an unprivileged attacker submit malformed-but-reachable RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs so `fetch_one_by_query_as` in `util/rich-indexer/src/store.rs` performs expensive or unsafe work before validation and amplify storage scans or proof generation with small crafted RPC requests, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rich-indexer/src/store.rs::fetch_one_by_query_as`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
