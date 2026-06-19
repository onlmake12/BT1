# Q1169: Critical crypto boundary divergence in gen_secret_key

## Question
Can an unprivileged attacker enter through a script author relying on secp/multisig/hash utilities through system script behavior and use script args, witness lock fields, pubkey hash matching, and conversion boundaries to drive `gen_secret_key` in `util/crypto/src/secp/generator.rs` across a boundary where panic or overrun a cryptographic parser before a malformed object is rejected, violating the invariant that cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/generator.rs::gen_secret_key`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
