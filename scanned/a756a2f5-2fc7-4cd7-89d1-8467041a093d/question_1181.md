# Q1181: Critical crypto boundary divergence in secp

## Question
Can an unprivileged attacker enter through a transaction sender supplying crafted signatures, hashes, script args, and witness data and use network-alert payload bytes, serialization format, byte order, and fixed-hash lengths to drive `secp` in `util/crypto/src/secp/mod.rs` across a boundary where panic or overrun a cryptographic parser before a malformed object is rejected, violating the invariant that system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/mod.rs::secp`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
