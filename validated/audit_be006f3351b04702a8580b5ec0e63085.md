The code is exactly as cited. Let me verify the Rust match semantics against the actual code.

In `kill_process` at lines 63–85 of `ckb-bin/src/subcommand/daemon.rs`:

```rust
match res {
    Ok(_) => {           // arm 1: exhaustively matches ALL Ok values
        wait_time -= 1;
        ...
    }
    _ if wait_time <= 0 => {   // arm 2: only reachable when res is Err
        kill(...SIGKILL...)?;
        break;
    }
    _ => { break; }            // arm 3: only reachable when res is Err
}
```

The claim is **correct**. In Rust, `Ok(_)` in arm 1 exhaustively matches every `Ok` variant. Arms 2 and 3 use `_` (catch-all), which only fires for values not matched by prior arms — i.e., only `Err` values. The guard `wait_time <= 0` in arm 2 is evaluated only when `check_process` returns `Err` (process already gone). While the process is alive and ignoring SIGTERM, arm 1 fires every iteration, `wait_time` decrements past zero without bound, and SIGKILL is never sent.

---

Audit Report

## Title
Infinite loop in `kill_process` when process ignores SIGTERM — (`ckb-bin/src/subcommand/daemon.rs`)

## Summary
The `kill_process` function sends SIGTERM and polls `check_process` in a loop, decrementing `wait_time` in the `Ok` arm. The force-kill guard `_ if wait_time <= 0` is placed in a catch-all arm that is only reachable when `check_process` returns `Err` (process already exited). If the process remains alive and ignores SIGTERM, `wait_time` decrements past zero indefinitely and SIGKILL is never sent, causing `ckb daemon --stop` to hang forever.

## Finding Description
In `kill_process` (`ckb-bin/src/subcommand/daemon.rs`, lines 61–86), `check_process` uses `kill(pid, None)` (signal 0) to probe liveness, returning `Ok(pid)` if the process exists and `Err` if it does not. The match has three arms:

- **Arm 1** (`Ok(_)`): fires whenever the process is alive; decrements `wait_time` and sleeps 1 second. Never checks `wait_time`.
- **Arm 2** (`_ if wait_time <= 0`): catch-all with guard; only reachable when `check_process` returns `Err` (process gone) AND `wait_time <= 0`.
- **Arm 3** (`_`): catch-all; only reachable when `check_process` returns `Err` and `wait_time > 0` — the normal clean-exit path.

Because arm 1 exhaustively consumes all `Ok` results, the SIGKILL escalation path (arm 2) is structurally unreachable while the process is alive. `wait_time` decrements without bound, the loop never breaks, and `ckb daemon --stop` hangs indefinitely. The existing 60-second timeout is entirely ineffective for its intended purpose.

## Impact Explanation
`ckb daemon --stop` hangs indefinitely when the target process defers or ignores SIGTERM. The operator loses the ability to stop the node through the normal CLI path. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local command line crash/hang**.

## Likelihood Explanation
Any CKB process that defers SIGTERM handling triggers this path — for example, during a long RocksDB flush, a blocked syscall, or a custom signal handler that delays shutdown. The operator runs `ckb daemon --stop`, SIGTERM is delivered but not acted on within 60 seconds, and the CLI hangs. This requires only local access to the node host and is a realistic operational scenario.

## Recommendation
Move the timeout check into the `Ok` arm so it is evaluated while the process is still alive:

```rust
Ok(_) => {
    wait_time -= 1;
    if wait_time <= 0 {
        kill(Pid::from_raw(pid), Some(Signal::SIGKILL)).map_err(|_| ExitCode::Failure)?;
        break;
    }
    eprint!("{}", ".".yellow());
    let _ = io::stderr().flush();
    std::thread::sleep(std::time::Duration::from_secs(1));
}
_ => { break; }
```

## Proof of Concept
Mock `check_process` to always return `Ok(pid)`. Call `kill_process`. Assert the loop exits within 61 iterations and that SIGKILL was sent. With the current code, the loop never exits and the assertion fails.

Alternatively: start a process that installs a SIGTERM handler that does nothing (`signal(SIGTERM, SIG_IGN)`), write its PID to a pid file, and run `ckb daemon --stop` — the CLI will hang indefinitely with dots printing forever. [1](#0-0)

### Citations

**File:** ckb-bin/src/subcommand/daemon.rs (L63-85)
```rust
        match res {
            Ok(_) => {
                wait_time -= 1;
                eprint!("{}", ".".yellow());
                let _ = io::stderr().flush();
                std::thread::sleep(std::time::Duration::from_secs(1));
            }
            _ if wait_time <= 0 => {
                eprintln!(
                    "{}",
                    format!(
                    "ckb daemon service is still running with pid {}..., stop it now forcefully ...",
                    pid
                )
                    .red()
                );
                kill(Pid::from_raw(pid), Some(Signal::SIGKILL)).map_err(|_| ExitCode::Failure)?;
                break;
            }
            _ => {
                break;
            }
        }
```
