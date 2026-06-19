# Q1307: Critical crypto canonical encoding ambiguity in $name

## Question
Can an unprivileged attacker craft alternate encodings for script args, witness lock fields, pubkey hash matching, and conversion boundaries through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `$name` in `util/fixed-hash/core/src/std_str.rs` accepts two representations for one security object and trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/fixed-hash/core/src/std_str.rs::$name`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
