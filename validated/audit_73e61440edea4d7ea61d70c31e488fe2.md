Looking at the actual code in `kill_process`:

The logic bug is real and demonstrable. Here is the analysis:

---

### Title
`kill_process` Timeout Guard Unreachable While Process Is Alive — Infinite Loop in `ckb daemon --stop` — (`ckb-bin/src/subcommand/daemon.rs`)

---

### Summary

The `kill_process` function contains a broken timeout mechanism. The force-kill guard `_ if wait_time <= 0` is structurally unreachable while the target process is still running, causing `ckb daemon --stop` to loop forever if the process does not exit after SIGTERM.

---

### Finding Description

The Rust `match` arms are evaluated in order:

```
Ok(_)              → process alive  → decrement wait_time, sleep, continue
_ if wait_time<=0  → only reachable when res is Err (not Ok)
_                  → process gone   → break
``` [1](#0-0) 

Because `Ok(_)` is listed first, it consumes every iteration where the process is alive. The guard `_ if wait_time <= 0` is only evaluated when `check_process` returns `Err` — i.e., when the process has **already exited**. At that point, arm 3 (`_ => break`) would fire anyway (unless `wait_time` somehow went negative before the process died, which is irrelevant to the force-kill intent).

The result: `wait_time` decrements from 60 to 0 to -1 to -2 … indefinitely, and SIGKILL is never sent while the process is alive. [2](#0-1) 

---

### Impact Explanation

Any local user who runs `ckb daemon --stop` against a CKB process that does not promptly exit after SIGTERM will have their CLI process hang forever. The CLI never returns, never sends SIGKILL, and never removes the PID file (the `fs::remove_file` call at line 23 is never reached). [3](#0-2) 

This fits the declared scope: **Any local command line crash/hang (0–500 points)**.

---

### Likelihood Explanation

A process that does not immediately exit on SIGTERM is a normal OS condition — the CKB process may be in an uninterruptible sleep (Linux D-state), performing a long flush, or simply slow to shut down. No custom signal handler or adversarial configuration is required. Any operator using `ckb daemon --stop` on a slow-to-stop node can reproduce this deterministically.

---

### Recommendation

Move the timeout check into the `Ok` arm, or restructure the loop to check `wait_time` unconditionally after each sleep:

```rust
Ok(_) => {
    wait_time -= 1;
    std::thread::sleep(std::time::Duration::from_secs(1));
    if wait_time <= 0 {
        // send SIGKILL here
        break;
    }
}
```

---

### Proof of Concept

1. Mock `check_process` to always return `Ok(pid)` (simulating a process that ignores SIGTERM).
2. Run `kill_process` with `wait_time = 60`.
3. Observe: the loop never exits — `wait_time` goes to 0, then negative, indefinitely.
4. The `SIGKILL` branch at line 79 is never reached.
5. The CLI hangs until killed externally. [4](#0-3)

### Citations

**File:** ckb-bin/src/subcommand/daemon.rs (L21-24)
```rust
    } else if args.stop {
        kill_process(pid_file, "ckb")?;
        fs::remove_file(pid_file).map_err(|_| ExitCode::Failure)?;
    }
```

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
