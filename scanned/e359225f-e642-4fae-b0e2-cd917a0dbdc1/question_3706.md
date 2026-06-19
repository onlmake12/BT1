# Q3706: Critical vm limit off by one in lib

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for malformed buffers, return codes, memory pointers, and script group composition through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `lib` in `script/src/lib.rs` make VM version gating select the wrong behavior at a hardfork boundary, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/lib.rs::lib`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
