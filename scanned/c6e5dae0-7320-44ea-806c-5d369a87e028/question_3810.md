# Q3810: High vm boundary divergence in resolved_inputs

## Question
Can an unprivileged attacker enter through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and use spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing to drive `resolved_inputs` in `script/src/syscalls/load_header.rs` across a boundary where make VM version gating select the wrong behavior at a hardfork boundary, violating the invariant that scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/load_header.rs::resolved_inputs`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
