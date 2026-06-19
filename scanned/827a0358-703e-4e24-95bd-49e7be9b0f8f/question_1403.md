# Q1403: High crypto boundary divergence in new

## Question
Can an unprivileged attacker enter through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths and use script args, witness lock fields, pubkey hash matching, and conversion boundaries to drive `new` in `util/network-alert/src/notifier.rs` across a boundary where trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating the invariant that system-script-visible crypto behavior must match consensus expectations, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/network-alert/src/notifier.rs::new`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
