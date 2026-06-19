# Q1294: Critical crypto limit off by one in fmt

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a script author relying on secp/multisig/hash utilities through system script behavior so `fmt` in `util/fixed-hash/core/src/std_fmt.rs` make verification accept a malformed signature/proof/hash that should be rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/fixed-hash/core/src/std_fmt.rs::fmt`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
