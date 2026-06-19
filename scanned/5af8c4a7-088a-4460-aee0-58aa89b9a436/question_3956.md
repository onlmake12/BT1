# Q3956: High vm boundary divergence in ecall

## Question
Can an unprivileged attacker enter through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes and use malformed buffers, return codes, memory pointers, and script group composition to drive `ecall` in `script/src/syscalls/write.rs` across a boundary where undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating the invariant that scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/write.rs::ecall`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
