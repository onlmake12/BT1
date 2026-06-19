# Q1168: Critical crypto resource amplification in gen_privkey

## Question
Can an unprivileged attacker repeatedly send small Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a script author relying on secp/multisig/hash utilities through system script behavior to make `gen_privkey` in `util/crypto/src/secp/generator.rs` amplify CPU, memory, storage, or bandwidth and make duplicate or empty proof elements produce a valid root for the wrong data, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/generator.rs::gen_privkey`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
