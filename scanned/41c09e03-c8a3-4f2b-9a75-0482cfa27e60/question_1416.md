# Q1416: Critical crypto batch interaction bug in VerifiableHeader

## Question
Can an unprivileged attacker batch network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a script author relying on secp/multisig/hash utilities through system script behavior so `VerifiableHeader` in `util/types/src/utilities/merkle_mountain_range.rs` handles the first item safely but applies incorrect assumptions to later items and make verification accept a malformed signature/proof/hash that should be rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/utilities/merkle_mountain_range.rs::VerifiableHeader`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
