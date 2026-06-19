# Q1396: High crypto replay reorder race in add

## Question
Can an unprivileged attacker replay, reorder, or delay public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings so `add` in `util/network-alert/src/notifier.rs` takes a stale branch and panic or overrun a cryptographic parser before a malformed object is rejected, breaking the invariant that cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/network-alert/src/notifier.rs::add`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
