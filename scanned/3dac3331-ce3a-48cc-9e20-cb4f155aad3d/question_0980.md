# Q980: High core boundary divergence in launch_onion_service

## Question
Can an unprivileged attacker enter through a block or transaction relayer triggering this helper during validation, sync, or storage updates and use serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values to drive `launch_onion_service` in `util/onion/src/onion_service.rs` across a boundary where break a resource bound or state transition that downstream modules assume is already enforced, violating the invariant that module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/onion/src/onion_service.rs::launch_onion_service`
- Entrypoint: a block or transaction relayer triggering this helper during validation, sync, or storage updates
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
