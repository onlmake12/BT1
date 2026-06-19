# Q1267: High crypto cache invalidation failure in AsRef

## Question
Can an unprivileged attacker use a block relayer supplying Merkle/MMR/proof-related data at boundary lengths to alternate valid and invalid public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings so `AsRef` in `util/fixed-hash/core/src/std_convert.rs` leaves a cache, index, or status flag stale and make duplicate or empty proof elements produce a valid root for the wrong data, violating system-script-visible crypto behavior must match consensus expectations, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_convert.rs::AsRef`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
