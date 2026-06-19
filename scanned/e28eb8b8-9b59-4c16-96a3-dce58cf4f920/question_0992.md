# Q992: Low core state transition mismatch in wait_tor_server_bootstrap_done

## Question
Can an unprivileged attacker enter through a local operator invoking a default-enabled node path that depends on this module and sequence serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values so `wait_tor_server_bootstrap_done` in `util/onion/src/tor_controller.rs` observes pre-state and post-state from different views, letting the flow trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/onion/src/tor_controller.rs::wait_tor_server_bootstrap_done`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
