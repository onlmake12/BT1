# Q3726: High vm resource amplification in ecall

## Question
Can an unprivileged attacker repeatedly send small malformed buffers, return codes, memory pointers, and script group composition through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes to make `ecall` in `script/src/syscalls/close.rs` amplify CPU, memory, storage, or bandwidth and trigger a VM panic or host-side bounds error before the transaction is rejected, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/close.rs::ecall`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
