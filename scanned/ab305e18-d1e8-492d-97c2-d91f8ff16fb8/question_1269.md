# Q1269: Critical crypto batch interaction bug in From

## Question
Can an unprivileged attacker batch public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `From` in `util/fixed-hash/core/src/std_convert.rs` handles the first item safely but applies incorrect assumptions to later items and make duplicate or empty proof elements produce a valid root for the wrong data, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_convert.rs::From`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
