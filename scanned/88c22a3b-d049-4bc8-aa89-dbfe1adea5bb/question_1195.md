# Q1195: Critical crypto state transition mismatch in sign_recoverable

## Question
Can an unprivileged attacker enter through a transaction sender supplying crafted signatures, hashes, script args, and witness data and sequence public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings so `sign_recoverable` in `util/crypto/src/secp/privkey.rs` observes pre-state and post-state from different views, letting the flow trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/privkey.rs::sign_recoverable`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
