# Q1413: High crypto state transition mismatch in verify_signatures

## Question
Can an unprivileged attacker enter through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings and sequence network-alert payload bytes, serialization format, byte order, and fixed-hash lengths so `verify_signatures` in `util/network-alert/src/verifier.rs` observes pre-state and post-state from different views, letting the flow make verification accept a malformed signature/proof/hash that should be rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/network-alert/src/verifier.rs::verify_signatures`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
