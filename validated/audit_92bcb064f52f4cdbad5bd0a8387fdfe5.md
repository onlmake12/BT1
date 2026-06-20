The relevant code is now fully visible. Let me analyze the exact crash path.

**`ckb-bin/src/helper.rs`, lines 48–60:**

```rust
pub fn prompt(msg: &str) -> String {
    let stdout = stdout();
    let mut stdout = stdout.lock();
    let stdin = stdin();

    write!(stdout, "{msg}").expect("failed to write prompt to stdout");  // line 53
    stdout.flush().expect("failed to flush stdout");                      // line 54
    ...
}
```

**`ckb-bin/src/subcommand/init.rs`, lines 51–54:**

```rust
if args.interactive {
    let in_block_assembler_code_hash = prompt("code hash: ");
    ...
}
```

---

### Title
Unhandled broken-pipe panic in `prompt()` causes `ckb init --interactive` to crash — (`ckb-bin/src/helper.rs`)

### Summary
`prompt()` calls `write!(stdout, …).expect(…)` and `stdout.flush().expect(…)`. Both `.expect()` calls **panic** (process abort) on any `io::Error`, including `EPIPE`. Running `ckb init -i | head -c 0` closes the read end of the pipe before the first `write!`, guaranteeing a broken-pipe error and a Rust panic.

### Finding Description
In `ckb-bin/src/helper.rs` the `prompt` function writes the prompt string to stdout using `write!` and then flushes, but propagates neither error — it calls `.expect()` on both: [1](#0-0) 

When stdout is a broken pipe, `write!` returns `Err(BrokenPipe)`. `.expect()` on an `Err` value unconditionally calls `panic!`, which terminates the process with a non-zero exit code and a panic backtrace on stderr. The same panic fires on the `flush` line if the `write!` somehow succeeds but the flush does not.

`init()` calls `prompt` up to four times in interactive mode: [2](#0-1) [3](#0-2) [4](#0-3) 

Every one of those call sites is reachable with a single shell command.

### Impact Explanation
The process terminates via an unhandled Rust panic instead of a clean, handled error exit. Within the stated scope ("Any local command line crash"), this is a concrete, reproducible crash of the `ckb` binary. Impact is limited to the CLI tool itself — no node state, no network effect, no data corruption.

### Likelihood Explanation
Trivially reproducible by any local user with execute permission on the `ckb` binary:

```
ckb init -i | head -c 0
```

No privileges, no special environment, no race condition required.

### Recommendation
Replace `.expect()` with proper error propagation. `prompt` should return `Result<String, io::Error>` (or use `io::Error` → `ExitCode` conversion), and callers in `init()` should use `?` or explicit error handling:

```rust
pub fn prompt(msg: &str) -> Result<String, io::Error> {
    let stdout = stdout();
    let mut stdout = stdout.lock();
    write!(stdout, "{msg}")?;
    stdout.flush()?;
    let mut input = String::new();
    stdin().read_line(&mut input)?;
    Ok(input)
}
```

### Proof of Concept
```sh
# Build CKB from source, then:
ckb init -i | head -c 0
# Expected (current): process panics with
#   thread 'main' panicked at 'failed to write prompt to stdout: Broken pipe'
# Expected (fixed): clean ExitCode::Failure with an error message
``` [5](#0-4) [6](#0-5)

### Citations

**File:** ckb-bin/src/helper.rs (L48-60)
```rust
pub fn prompt(msg: &str) -> String {
    let stdout = stdout();
    let mut stdout = stdout.lock();
    let stdin = stdin();

    write!(stdout, "{msg}").expect("failed to write prompt to stdout");
    stdout.flush().expect("failed to flush stdout");

    let mut input = String::new();
    let _ = stdin.read_line(&mut input);

    input
}
```

**File:** ckb-bin/src/subcommand/init.rs (L40-54)
```rust
        if args.interactive {
            let input = prompt("Overwrite config files now? ");

            if !["y", "Y"].contains(&input.trim()) {
                return Err(ExitCode::Failure);
            }
        } else {
            return Err(ExitCode::Failure);
        }
    }

    if args.interactive {
        let in_block_assembler_code_hash = prompt("code hash: ");
        let in_args = prompt("args: ");
        let in_hash_type = prompt("hash_type: ");
```

**File:** ckb-bin/src/subcommand/init.rs (L79-79)
```rust
        let in_message = prompt("message: ");
```
