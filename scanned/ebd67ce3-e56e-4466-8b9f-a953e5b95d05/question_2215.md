# Q2215: High rpc differential path split in lib

## Question
Can an unprivileged attacker reach `lib` in `util/indexer/src/lib.rs` through two production paths from a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs and make one path accept while the other rejects because of JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/indexer/src/lib.rs::lib`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
