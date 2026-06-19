# Q1279: High crypto parser precheck gap in Default

## Question
Can an unprivileged attacker submit malformed-but-reachable Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `Default` in `util/fixed-hash/core/src/std_default.rs` performs expensive or unsafe work before validation and make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_default.rs::Default`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
