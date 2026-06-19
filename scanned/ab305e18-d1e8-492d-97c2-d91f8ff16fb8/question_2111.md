# Q2111: High rpc parser precheck gap in get_cells_capacity

## Question
Can an unprivileged attacker submit malformed-but-reachable RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `get_cells_capacity` in `rpc/src/module/indexer.rs` performs expensive or unsafe work before validation and amplify storage scans or proof generation with small crafted RPC requests, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/module/indexer.rs::get_cells_capacity`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
