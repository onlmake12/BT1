# Q1381: Critical crypto restart reorg persistence in mark_as_known

## Question
Can an unprivileged attacker shape Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a transaction sender supplying crafted signatures, hashes, script args, and witness data, then force normal restart, reorg, retry, or replay handling so `mark_as_known` in `util/network-alert/src/alert_relayer.rs` persists inconsistent state and trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/network-alert/src/alert_relayer.rs::mark_as_known`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
