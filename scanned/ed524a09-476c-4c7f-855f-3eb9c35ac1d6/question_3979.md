# Q3979: High vm limit off by one in vm_version

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles so `vm_version` in `script/src/types.rs` trigger a VM panic or host-side bounds error before the transaction is rejected, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/types.rs::vm_version`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
