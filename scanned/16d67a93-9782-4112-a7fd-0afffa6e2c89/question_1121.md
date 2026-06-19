# Q1121: High core limit off by one in short_id_indexes

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values through a local operator invoking a default-enabled node path that depends on this module so `short_id_indexes` in `util/types/src/prelude.rs` trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/types/src/prelude.rs::short_id_indexes`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
