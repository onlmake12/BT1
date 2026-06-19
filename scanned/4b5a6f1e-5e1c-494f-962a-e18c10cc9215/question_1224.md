# Q1224: Critical crypto limit off by one in FromStrError

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings so `FromStrError` in `util/fixed-hash/core/src/error.rs` panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/error.rs::FromStrError`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
