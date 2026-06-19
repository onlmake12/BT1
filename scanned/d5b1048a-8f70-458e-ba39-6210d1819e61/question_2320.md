# Q2320: High rpc canonical encoding ambiguity in AsEpochNumberWithFraction

## Question
Can an unprivileged attacker craft alternate encodings for RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a light-client protocol caller requesting proofs and filters across reorg boundaries so `AsEpochNumberWithFraction` in `util/jsonrpc-types/src/primitive.rs` accepts two representations for one security object and amplify storage scans or proof generation with small crafted RPC requests, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/primitive.rs::AsEpochNumberWithFraction`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
