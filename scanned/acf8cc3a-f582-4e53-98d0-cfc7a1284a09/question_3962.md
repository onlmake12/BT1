# Q3962: High vm cache invalidation failure in type_id_creation_hash_uses_first_input_and_first_...

## Question
Can an unprivileged attacker use a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles to alternate valid and invalid RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors so `type_id_creation_hash_uses_first_input_and_first_output_index` in `script/src/type_id.rs` leaves a cache, index, or status flag stale and make VM version gating select the wrong behavior at a hardfork boundary, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/type_id.rs::type_id_creation_hash_uses_first_input_and_first_output_index`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
