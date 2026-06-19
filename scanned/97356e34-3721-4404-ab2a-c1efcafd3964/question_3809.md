# Q3809: High vm limit off by one in resolved_cell_deps

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for malformed buffers, return codes, memory pointers, and script group composition through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles so `resolved_cell_deps` in `script/src/syscalls/load_header.rs` make VM version gating select the wrong behavior at a hardfork boundary, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/load_header.rs::resolved_cell_deps`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
