# Q1022: High core limit off by one in shrink_to_fit

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs through a script or network payload causing production code to parse, convert, or cache attacker-shaped data so `shrink_to_fit` in `util/src/shrink_to_fit.rs` trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/src/shrink_to_fit.rs::shrink_to_fit`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
