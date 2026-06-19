# Q3710: High vm limit off by one in lib

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles so `lib` in `script/src/lib.rs` make a syscall expose bytes that differ from consensus-resolved transaction data, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/lib.rs::lib`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
