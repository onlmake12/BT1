# Q1190: High crypto state transition mismatch in from

## Question
Can an unprivileged attacker enter through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths and sequence public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings so `from` in `util/crypto/src/secp/privkey.rs` observes pre-state and post-state from different views, letting the flow panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/privkey.rs::from`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
