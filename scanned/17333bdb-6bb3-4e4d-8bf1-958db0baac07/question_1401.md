# Q1401: Critical crypto parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `new` in `util/network-alert/src/notifier.rs` performs expensive or unsafe work before validation and trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/network-alert/src/notifier.rs::new`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
