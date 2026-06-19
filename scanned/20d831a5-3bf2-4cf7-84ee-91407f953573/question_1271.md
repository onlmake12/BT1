# Q1271: High crypto batch interaction bug in From

## Question
Can an unprivileged attacker batch Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a script author relying on secp/multisig/hash utilities through system script behavior so `From` in `util/fixed-hash/core/src/std_convert.rs` handles the first item safely but applies incorrect assumptions to later items and make duplicate or empty proof elements produce a valid root for the wrong data, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_convert.rs::From`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
