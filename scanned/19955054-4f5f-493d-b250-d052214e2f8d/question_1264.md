# Q1264: Critical crypto limit off by one in partial_cmp

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings so `partial_cmp` in `util/fixed-hash/core/src/std_cmp.rs` panic or overrun a cryptographic parser before a malformed object is rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_cmp.rs::partial_cmp`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
