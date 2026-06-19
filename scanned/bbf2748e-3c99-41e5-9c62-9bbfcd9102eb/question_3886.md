# Q3886: High vm batch interaction bug in ecall

## Question
Can an unprivileged attacker batch spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `ecall` in `script/src/syscalls/pipe.rs` handles the first item safely but applies incorrect assumptions to later items and undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/pipe.rs::ecall`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
