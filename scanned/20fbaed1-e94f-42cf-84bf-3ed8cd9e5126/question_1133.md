# Q1133: Low core differential path split in compact_to_target

## Question
Can an unprivileged attacker reach `compact_to_target` in `util/types/src/utilities/difficulty.rs` through two production paths from a script or network payload causing production code to parse, convert, or cache attacker-shaped data and make one path accept while the other rejects because of local config or RPC parameters that flow into production node behavior, violating shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/types/src/utilities/difficulty.rs::compact_to_target`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
