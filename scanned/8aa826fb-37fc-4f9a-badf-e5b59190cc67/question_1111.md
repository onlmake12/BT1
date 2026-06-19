# Q1111: High core boundary divergence in lib

## Question
Can an unprivileged attacker enter through a local operator invoking a default-enabled node path that depends on this module and use serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values to drive `lib` in `util/types/src/lib.rs` across a boundary where make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating the invariant that module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/types/src/lib.rs::lib`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
