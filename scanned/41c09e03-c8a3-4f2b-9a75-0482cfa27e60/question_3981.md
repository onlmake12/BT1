# Q3981: Critical vm replay reorder race in complete

## Question
Can an unprivileged attacker replay, reorder, or delay spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `complete` in `script/src/verify.rs` takes a stale branch and trigger a VM panic or host-side bounds error before the transaction is rejected, breaking the invariant that CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/verify.rs::complete`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
