# Q1074: High core differential path split in From

## Question
Can an unprivileged attacker reach `From` in `util/types/src/conversion/storage.rs` through two production paths from a block or transaction relayer triggering this helper during validation, sync, or storage updates and make one path accept while the other rejects because of serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/types/src/conversion/storage.rs::From`
- Entrypoint: a block or transaction relayer triggering this helper during validation, sync, or storage updates
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
