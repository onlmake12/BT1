# Q2078: High rpc canonical encoding ambiguity in lib

## Question
Can an unprivileged attacker craft alternate encodings for indexer state freshness, reorg timing, block-filter requests, and proof target positions through a light-client protocol caller requesting proofs and filters across reorg boundaries so `lib` in `block-filter/src/lib.rs` accepts two representations for one security object and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `block-filter/src/lib.rs::lib`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
