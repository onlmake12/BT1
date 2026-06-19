# Q1380: Critical crypto differential path split in mark_as_known

## Question
Can an unprivileged attacker reach `mark_as_known` in `util/network-alert/src/alert_relayer.rs` through two production paths from a peer relaying network alerts or consensus objects with adversarial cryptographic encodings and make one path accept while the other rejects because of script args, witness lock fields, pubkey hash matching, and conversion boundaries, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/network-alert/src/alert_relayer.rs::mark_as_known`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
