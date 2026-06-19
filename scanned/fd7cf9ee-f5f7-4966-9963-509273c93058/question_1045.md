# Q1045: Low core resource amplification in From

## Question
Can an unprivileged attacker repeatedly send small local config or RPC parameters that flow into production node behavior through a script or network payload causing production code to parse, convert, or cache attacker-shaped data to make `From` in `util/types/src/block_number_and_hash.rs` amplify CPU, memory, storage, or bandwidth and break a resource bound or state transition that downstream modules assume is already enforced, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/types/src/block_number_and_hash.rs::From`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
