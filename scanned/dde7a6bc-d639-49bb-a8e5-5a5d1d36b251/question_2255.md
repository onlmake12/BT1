# Q2255: High rpc canonical encoding ambiguity in json_schema

## Question
Can an unprivileged attacker craft alternate encodings for block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `json_schema` in `util/jsonrpc-types/src/bytes.rs` accepts two representations for one security object and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/bytes.rs::json_schema`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
