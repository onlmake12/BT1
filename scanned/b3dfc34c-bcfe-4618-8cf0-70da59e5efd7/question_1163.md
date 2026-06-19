# Q1163: High crypto restart reorg persistence in from

## Question
Can an unprivileged attacker shape Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths, then force normal restart, reorg, retry, or replay handling so `from` in `util/crypto/src/secp/error.rs` persists inconsistent state and make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/error.rs::from`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
