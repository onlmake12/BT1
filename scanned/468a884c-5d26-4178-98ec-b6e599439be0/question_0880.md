# Q880: High core cache invalidation failure in is_utf8

## Question
Can an unprivileged attacker use a script or network payload causing production code to parse, convert, or cache attacker-shaped data to alternate valid and invalid serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values so `is_utf8` in `util/gen-types/src/conversion/primitive.rs` leaves a cache, index, or status flag stale and make canonical serialization or conversion accept an ambiguous representation, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/gen-types/src/conversion/primitive.rs::is_utf8`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
