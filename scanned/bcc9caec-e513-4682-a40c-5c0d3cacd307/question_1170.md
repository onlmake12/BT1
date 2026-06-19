# Q1170: Critical crypto replay reorder race in new

## Question
Can an unprivileged attacker replay, reorder, or delay Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `new` in `util/crypto/src/secp/generator.rs` takes a stale branch and make duplicate or empty proof elements produce a valid root for the wrong data, breaking the invariant that system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/generator.rs::new`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
