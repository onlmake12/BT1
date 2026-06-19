# Q2302: Low rpc differential path split in schema_name

## Question
Can an unprivileged attacker reach `schema_name` in `util/jsonrpc-types/src/json_schema.rs` through two production paths from a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs and make one path accept while the other rejects because of JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/json_schema.rs::schema_name`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
