# Q3960: Critical vm batch interaction bug in new

## Question
Can an unprivileged attacker batch malformed buffers, return codes, memory pointers, and script group composition through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `new` in `script/src/syscalls/write.rs` handles the first item safely but applies incorrect assumptions to later items and undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/write.rs::new`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
