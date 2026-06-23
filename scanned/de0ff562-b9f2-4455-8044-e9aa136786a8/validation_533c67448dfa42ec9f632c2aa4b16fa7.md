The code is present and readable. Let me analyze the actual logic carefully.

The logic bug is real and concrete. Here is the analysis:

---

### Title
Infinite loop in `kill_process` when target process ignores SIGTERM — (`ckb-bin/src/subcommand/daemon.rs`)

### Summary

The `kill_process` function contains a misplaced timeout guard. The `wait_time <= 0` check is placed in the `Err` match arm (process already gone), making it unreachable when the process is still alive. If the target process never terminates, the loop runs forever.

### Finding Description

The loop structure at lines 61–86:

```rust
let mut wait_time = 60;
loop {
    let res = check_process(pid_file);
    match res {
        Ok(_) => {
            wait_time -= 1;          // ← decrements, but no timeout check here
            std::thread::sleep(...);
        }
        _ if wait_time <= 0 => {     // ← only evaluated when check_process returns Err
            kill(...SIGKILL...)?;
            break;
        }
        _ => { break; }
    }
}
``` [1](#0-0) 

The `Ok(_)` arm matches whenever `check_process` returns `Ok` (process still alive). In that arm, `wait_time` is decremented but **the `wait_time <= 0` guard is never evaluated** — it only fires in the `Err` branch (process already gone). Once `wait_time` reaches 0 and goes negative, the `Ok(_)` arm continues to match unconditionally, and the force-kill path at line 79 is never reached. [2](#0-1) 

The `_ if wait_time <= 0` arm is effectively dead code in the scenario it was designed for (process still alive after timeout).

### Impact Explanation

`ckb daemon --stop` hangs indefinitely. The operator loses the ability to stop the node through the normal CLI path. The node process itself is unaffected, but the CLI process consumes a thread sleeping in a 1-second loop forever. Scope: local command-line hang (0–500 pts).

### Likelihood Explanation

Any CKB process that defers or ignores SIGTERM (e.g., during a long flush, a custom signal handler, or a blocked syscall) triggers this path. This is a realistic operational scenario, not a contrived one.

### Recommendation

Move the timeout check into the `Ok` arm:

```rust
Ok(_) => {
    wait_time -= 1;
    if wait_time <= 0 {
        // force kill
        kill(Pid::from_raw(pid), Some(Signal::SIGKILL))...;
        break;
    }
    std::thread::sleep(...);
}
_ => { break; }
``` [3](#0-2) 

### Proof of Concept

Mock `check_process` to always return `Ok(pid)`. Run `kill_process`. Assert the loop exits after at most 60 iterations and that `SIGKILL` is sent. With the current code, the assertion fails — the loop never exits.

### Citations

**File:** ckb-bin/src/subcommand/daemon.rs (L59-86)
```rust
    let mut wait_time = 60;
    eprintln!("{}", "waiting ckb service to stop ...".yellow());
    loop {
        let res = check_process(pid_file);
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
    }
```
