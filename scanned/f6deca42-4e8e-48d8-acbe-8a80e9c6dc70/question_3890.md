# Q3890: High vm state transition mismatch in new

## Question
Can an unprivileged attacker enter through a transaction sender deploying a crafted CKB-VM script and witness payload and sequence spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing so `new` in `script/src/syscalls/pipe.rs` observes pre-state and post-state from different views, letting the flow make a syscall expose bytes that differ from consensus-resolved transaction data, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/pipe.rs::new`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
