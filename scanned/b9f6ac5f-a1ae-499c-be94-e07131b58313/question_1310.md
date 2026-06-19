# Q1310: High crypto restart reorg persistence in FromStr

## Question
Can an unprivileged attacker shape public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a transaction sender supplying crafted signatures, hashes, script args, and witness data, then force normal restart, reorg, retry, or replay handling so `FromStr` in `util/fixed-hash/core/src/std_str.rs` persists inconsistent state and make verification accept a malformed signature/proof/hash that should be rejected, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_str.rs::FromStr`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
