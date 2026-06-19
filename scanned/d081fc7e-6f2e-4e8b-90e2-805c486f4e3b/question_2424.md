# Q2424: High rpc canonical encoding ambiguity in lib

## Question
Can an unprivileged attacker craft alternate encodings for indexer state freshness, reorg timing, block-filter requests, and proof target positions through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `lib` in `util/rich-indexer/src/lib.rs` accepts two representations for one security object and amplify storage scans or proof generation with small crafted RPC requests, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rich-indexer/src/lib.rs::lib`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
