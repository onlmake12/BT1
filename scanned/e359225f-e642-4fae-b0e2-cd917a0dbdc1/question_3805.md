# Q3805: High vm boundary divergence in header_deps

## Question
Can an unprivileged attacker enter through a transaction sender deploying a crafted CKB-VM script and witness payload and use spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing to drive `header_deps` in `script/src/syscalls/load_header.rs` across a boundary where make VM version gating select the wrong behavior at a hardfork boundary, violating the invariant that cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/load_header.rs::header_deps`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
