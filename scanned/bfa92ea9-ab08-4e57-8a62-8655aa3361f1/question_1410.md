# Q1410: Critical crypto parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings so `new` in `util/network-alert/src/verifier.rs` performs expensive or unsafe work before validation and trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/network-alert/src/verifier.rs::new`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
