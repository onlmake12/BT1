# Q1103: High core canonical encoding ambiguity in global

## Question
Can an unprivileged attacker craft alternate encodings for serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values through a script or network payload causing production code to parse, convert, or cache attacker-shaped data so `global` in `util/types/src/global.rs` accepts two representations for one security object and make canonical serialization or conversion accept an ambiguous representation, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/types/src/global.rs::global`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
