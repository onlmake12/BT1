# Q1250: Critical crypto differential path split in expecting

## Question
Can an unprivileged attacker reach `expecting` in `util/fixed-hash/core/src/serde.rs` through two production paths from a transaction sender supplying crafted signatures, hashes, script args, and witness data and make one path accept while the other rejects because of network-alert payload bytes, serialization format, byte order, and fixed-hash lengths, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/serde.rs::expecting`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
