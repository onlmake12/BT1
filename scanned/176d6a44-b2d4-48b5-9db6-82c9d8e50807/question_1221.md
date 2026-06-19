# Q1221: Critical crypto replay reorder race in FromStrError

## Question
Can an unprivileged attacker replay, reorder, or delay network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `FromStrError` in `util/fixed-hash/core/src/error.rs` takes a stale branch and make duplicate or empty proof elements produce a valid root for the wrong data, breaking the invariant that hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/error.rs::FromStrError`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
