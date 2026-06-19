# Q882: Low core limit off by one in From

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs through a script or network payload causing production code to parse, convert, or cache attacker-shaped data so `From` in `util/gen-types/src/conversion/utilities.rs` break a resource bound or state transition that downstream modules assume is already enforced, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/gen-types/src/conversion/utilities.rs::From`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
