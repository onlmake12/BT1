# Q805: High core resource amplification in mainnet

## Question
Can an unprivileged attacker repeatedly send small message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs through a local operator invoking a default-enabled node path that depends on this module to make `mainnet` in `util/constant/src/softfork/mainnet.rs` amplify CPU, memory, storage, or bandwidth and break a resource bound or state transition that downstream modules assume is already enforced, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/constant/src/softfork/mainnet.rs::mainnet`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
