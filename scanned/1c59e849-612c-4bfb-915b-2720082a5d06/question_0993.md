# Q993: High core differential path split in wait_tor_server_bootstrap_done

## Question
Can an unprivileged attacker reach `wait_tor_server_bootstrap_done` in `util/onion/src/tor_controller.rs` through two production paths from a local operator invoking a default-enabled node path that depends on this module and make one path accept while the other rejects because of local config or RPC parameters that flow into production node behavior, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/onion/src/tor_controller.rs::wait_tor_server_bootstrap_done`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
