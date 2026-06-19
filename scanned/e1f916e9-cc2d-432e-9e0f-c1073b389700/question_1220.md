# Q1220: Critical crypto cross module inconsistency in FromStrError

## Question
Can an unprivileged attacker use a script author relying on secp/multisig/hash utilities through system script behavior to make `FromStrError` in `util/fixed-hash/core/src/error.rs` return a result that downstream modules interpret differently, where make duplicate or empty proof elements produce a valid root for the wrong data, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/fixed-hash/core/src/error.rs::FromStrError`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
