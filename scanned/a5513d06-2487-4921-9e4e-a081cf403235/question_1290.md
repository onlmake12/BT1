# Q1290: High crypto batch interaction bug in Display

## Question
Can an unprivileged attacker batch script args, witness lock fields, pubkey hash matching, and conversion boundaries through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `Display` in `util/fixed-hash/core/src/std_fmt.rs` handles the first item safely but applies incorrect assumptions to later items and make duplicate or empty proof elements produce a valid root for the wrong data, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_fmt.rs::Display`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
