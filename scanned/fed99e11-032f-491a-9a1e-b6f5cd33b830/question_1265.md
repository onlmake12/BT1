# Q1265: Critical crypto differential path split in partial_cmp

## Question
Can an unprivileged attacker reach `partial_cmp` in `util/fixed-hash/core/src/std_cmp.rs` through two production paths from a peer relaying network alerts or consensus objects with adversarial cryptographic encodings and make one path accept while the other rejects because of public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_cmp.rs::partial_cmp`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
