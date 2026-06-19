# Q1418: Critical crypto cross module inconsistency in from

## Question
Can an unprivileged attacker use a transaction sender supplying crafted signatures, hashes, script args, and witness data to make `from` in `util/types/src/utilities/merkle_mountain_range.rs` return a result that downstream modules interpret differently, where make verification accept a malformed signature/proof/hash that should be rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/utilities/merkle_mountain_range.rs::from`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
