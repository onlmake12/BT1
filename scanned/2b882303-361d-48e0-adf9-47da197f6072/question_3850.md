# Q3850: Critical vm boundary divergence in new

## Question
Can an unprivileged attacker enter through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes and use spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing to drive `new` in `script/src/syscalls/load_tx.rs` across a boundary where undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating the invariant that CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/load_tx.rs::new`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
