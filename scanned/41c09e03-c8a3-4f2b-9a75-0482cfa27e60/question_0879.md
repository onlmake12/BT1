# Q879: High core state transition mismatch in is_utf8

## Question
Can an unprivileged attacker enter through a local operator invoking a default-enabled node path that depends on this module and sequence serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values so `is_utf8` in `util/gen-types/src/conversion/primitive.rs` observes pre-state and post-state from different views, letting the flow trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/gen-types/src/conversion/primitive.rs::is_utf8`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
