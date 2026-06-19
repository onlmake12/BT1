# Q14: Low cli differential path split in run_daemon

## Question
Can an unprivileged attacker reach `run_daemon` in `ckb-bin/src/lib.rs` through two production paths from a local command-line user invoking supported CKB subcommands with crafted arguments and make one path accept while the other rejects because of runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `ckb-bin/src/lib.rs::run_daemon`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
