# Q1204: High crypto resource amplification in from

## Question
Can an unprivileged attacker repeatedly send small network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings to make `from` in `util/crypto/src/secp/pubkey.rs` amplify CPU, memory, storage, or bandwidth and panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/pubkey.rs::from`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
