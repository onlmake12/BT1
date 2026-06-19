# Q1412: Critical crypto differential path split in verify_signatures

## Question
Can an unprivileged attacker reach `verify_signatures` in `util/network-alert/src/verifier.rs` through two production paths from a transaction sender supplying crafted signatures, hashes, script args, and witness data and make one path accept while the other rejects because of network-alert payload bytes, serialization format, byte order, and fixed-hash lengths, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/network-alert/src/verifier.rs::verify_signatures`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
