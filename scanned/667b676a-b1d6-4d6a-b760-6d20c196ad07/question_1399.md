# Q1399: Critical crypto canonical encoding ambiguity in clear_expired_alerts

## Question
Can an unprivileged attacker craft alternate encodings for script args, witness lock fields, pubkey hash matching, and conversion boundaries through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `clear_expired_alerts` in `util/network-alert/src/notifier.rs` accepts two representations for one security object and trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/network-alert/src/notifier.rs::clear_expired_alerts`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
