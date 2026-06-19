# Q1099: High core canonical encoding ambiguity in global

## Question
Can an unprivileged attacker craft alternate encodings for serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values through a local operator invoking a default-enabled node path that depends on this module so `global` in `util/types/src/global.rs` accepts two representations for one security object and make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/types/src/global.rs::global`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
