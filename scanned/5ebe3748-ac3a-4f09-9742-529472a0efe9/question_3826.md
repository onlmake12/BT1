# Q3826: High vm limit off by one in Syscalls

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for malformed buffers, return codes, memory pointers, and script group composition through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles so `Syscalls` in `script/src/syscalls/load_script.rs` make a syscall expose bytes that differ from consensus-resolved transaction data, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/load_script.rs::Syscalls`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
