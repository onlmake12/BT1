# Q732: High core limit off by one in get_commit_describe

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `get_commit_describe` in `util/build-info/src/lib.rs` trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/build-info/src/lib.rs::get_commit_describe`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
