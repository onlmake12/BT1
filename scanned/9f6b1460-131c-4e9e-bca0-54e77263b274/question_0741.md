# Q741: High core batch interaction bug in is_empty

## Question
Can an unprivileged attacker batch serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `is_empty` in `util/chain-iter/src/lib.rs` handles the first item safely but applies incorrect assumptions to later items and make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/chain-iter/src/lib.rs::is_empty`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
