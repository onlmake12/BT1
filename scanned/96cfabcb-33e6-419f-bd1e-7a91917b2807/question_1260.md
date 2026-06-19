# Q1260: Critical crypto cross module inconsistency in PartialEq

## Question
Can an unprivileged attacker use a transaction sender supplying crafted signatures, hashes, script args, and witness data to make `PartialEq` in `util/fixed-hash/core/src/std_cmp.rs` return a result that downstream modules interpret differently, where make verification accept a malformed signature/proof/hash that should be rejected, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_cmp.rs::PartialEq`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
