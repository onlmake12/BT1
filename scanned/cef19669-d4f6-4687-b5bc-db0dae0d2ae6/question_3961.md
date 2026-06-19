# Q3961: Critical vm batch interaction bug in TypeIdSystemScript

## Question
Can an unprivileged attacker batch malformed buffers, return codes, memory pointers, and script group composition through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `TypeIdSystemScript` in `script/src/type_id.rs` handles the first item safely but applies incorrect assumptions to later items and make a syscall expose bytes that differ from consensus-resolved transaction data, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/type_id.rs::TypeIdSystemScript`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
