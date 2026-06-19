# Q1407: High crypto limit off by one in Verifier

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for script args, witness lock fields, pubkey hash matching, and conversion boundaries through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings so `Verifier` in `util/network-alert/src/verifier.rs` panic or overrun a cryptographic parser before a malformed object is rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/network-alert/src/verifier.rs::Verifier`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
