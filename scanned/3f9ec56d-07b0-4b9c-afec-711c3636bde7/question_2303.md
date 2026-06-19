# Q2303: Low rpc parser precheck gap in u256_json_schema

## Question
Can an unprivileged attacker submit malformed-but-reachable RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `u256_json_schema` in `util/jsonrpc-types/src/json_schema.rs` performs expensive or unsafe work before validation and amplify storage scans or proof generation with small crafted RPC requests, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/json_schema.rs::u256_json_schema`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
