# Q1289: High crypto resource amplification in Debug

## Question
Can an unprivileged attacker repeatedly send small Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a transaction sender supplying crafted signatures, hashes, script args, and witness data to make `Debug` in `util/fixed-hash/core/src/std_fmt.rs` amplify CPU, memory, storage, or bandwidth and panic or overrun a cryptographic parser before a malformed object is rejected, violating system-script-visible crypto behavior must match consensus expectations, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_fmt.rs::Debug`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
