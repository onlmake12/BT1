# Q1379: Critical crypto resource amplification in clear_expired_alerts

## Question
Can an unprivileged attacker repeatedly send small public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths to make `clear_expired_alerts` in `util/network-alert/src/alert_relayer.rs` amplify CPU, memory, storage, or bandwidth and trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/network-alert/src/alert_relayer.rs::clear_expired_alerts`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
