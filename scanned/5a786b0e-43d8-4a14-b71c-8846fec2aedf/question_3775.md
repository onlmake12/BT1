# Q3775: Critical vm limit off by one in generate_ckb_syscalls

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for malformed buffers, return codes, memory pointers, and script group composition through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `generate_ckb_syscalls` in `script/src/syscalls/generator.rs` make VM version gating select the wrong behavior at a hardfork boundary, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/generator.rs::generate_ckb_syscalls`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
