# Q1312: Critical crypto resource amplification in from_trimmed_str

## Question
Can an unprivileged attacker repeatedly send small network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a script author relying on secp/multisig/hash utilities through system script behavior to make `from_trimmed_str` in `util/fixed-hash/core/src/std_str.rs` amplify CPU, memory, storage, or bandwidth and trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_str.rs::from_trimmed_str`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
