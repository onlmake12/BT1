# Q1415: Critical crypto canonical encoding ambiguity in verify_signatures

## Question
Can an unprivileged attacker craft alternate encodings for script args, witness lock fields, pubkey hash matching, and conversion boundaries through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `verify_signatures` in `util/network-alert/src/verifier.rs` accepts two representations for one security object and panic or overrun a cryptographic parser before a malformed object is rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/network-alert/src/verifier.rs::verify_signatures`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
