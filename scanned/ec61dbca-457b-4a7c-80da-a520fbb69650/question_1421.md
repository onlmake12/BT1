# Q1421: Critical crypto restart reorg persistence in set_last_header

## Question
Can an unprivileged attacker shape network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings, then force normal restart, reorg, retry, or replay handling so `set_last_header` in `util/types/src/utilities/merkle_mountain_range.rs` persists inconsistent state and make duplicate or empty proof elements produce a valid root for the wrong data, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/utilities/merkle_mountain_range.rs::set_last_header`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
