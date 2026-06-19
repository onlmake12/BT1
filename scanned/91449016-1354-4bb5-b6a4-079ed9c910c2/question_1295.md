# Q1295: Critical crypto boundary divergence in fmt

## Question
Can an unprivileged attacker enter through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings and use network-alert payload bytes, serialization format, byte order, and fixed-hash lengths to drive `fmt` in `util/fixed-hash/core/src/std_fmt.rs` across a boundary where make verification accept a malformed signature/proof/hash that should be rejected, violating the invariant that cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_fmt.rs::fmt`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
