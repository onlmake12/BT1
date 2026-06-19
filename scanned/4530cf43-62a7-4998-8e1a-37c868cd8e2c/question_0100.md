# Q100: High cli resource amplification in Template

## Question
Can an unprivileged attacker repeatedly send small local database contents, malformed config files, and supported operator commands through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data to make `Template` in `resource/src/template.rs` amplify CPU, memory, storage, or bandwidth and crash the command or node through supported local input before validation or recovery runs, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `resource/src/template.rs::Template`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
