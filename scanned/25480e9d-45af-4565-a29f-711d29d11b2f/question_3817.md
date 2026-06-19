# Q3817: High vm differential path split in load_by_field

## Question
Can an unprivileged attacker reach `load_by_field` in `script/src/syscalls/load_input.rs` through two production paths from a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and make one path accept while the other rejects because of spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/load_input.rs::load_by_field`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
