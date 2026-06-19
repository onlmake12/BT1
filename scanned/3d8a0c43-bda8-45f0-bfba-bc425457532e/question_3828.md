# Q3828: High vm resource amplification in ecall

## Question
Can an unprivileged attacker repeatedly send small malformed buffers, return codes, memory pointers, and script group composition through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes to make `ecall` in `script/src/syscalls/load_script.rs` amplify CPU, memory, storage, or bandwidth and undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/load_script.rs::ecall`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
