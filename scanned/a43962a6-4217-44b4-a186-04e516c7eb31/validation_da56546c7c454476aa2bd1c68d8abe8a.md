The code is confirmed exactly as cited. The logic analysis is correct.

In Rust's `match`, arms are evaluated in order against the discriminant. When `check_process` returns `Ok(_)`, arm 1 always matches — `wait_time` is decremented but the `wait_time <= 0` guard in arm 2 is **never evaluated** because arm 2 only fires when `check_process` returns `Err`. The force-kill path at line 79 is unreachable while the process is alive, regardless of how negative `wait_time` goes. [1](#0-0) 

---

Audit Report

## Title
Infinite loop in `kill_process` when process ignores SIGTERM — (`ckb-bin/src/subcommand/daemon.rs`)

## Summary
The `kill_process` function sends SIGTERM and then polls `check_process` in a loop, decrementing `wait_time` in the `Ok` arm. However, the force-kill guard `_ if wait_time <= 0` is placed in a catch-all arm that only fires when `check_process` returns `Err` (process already gone). If the process remains alive and ignores SIGTERM, `wait_time` decrements past zero indefinitely and SIGKILL is never sent, causing `ckb daemon --stop` to hang forever.

## Finding Description
In `kill_process` (lines 61–86 of `ckb-bin/src/subcommand/daemon.rs`), the match arms are:

1. `Ok(_)` — fires when the process is still alive; decrements `wait_time` and sleeps 1 second.
2. `_ if wait_time <= 0` — fires only when `check_process` returns `Err` AND `wait_time <= 0`.
3. `_` — fires when `check_process` returns `Err` and `wait_time > 0` (normal exit).

Because arm 1 exhaustively matches all `Ok` results, arms 2 and 3 are only reachable when the process has already exited. The intended timeout logic — escalate to SIGKILL after 60 seconds — is structurally unreachable in the scenario it was designed for. `wait_time` goes negative without bound, and the loop never breaks. [2](#0-1) [3](#0-2) 

## Impact Explanation
`ckb daemon --stop` hangs indefinitely when the target process defers or ignores SIGTERM. The operator loses the ability to stop the node through the normal CLI path. This is a local command-line hang, matching the allowed CKB bounty impact: **Note (0–500 points) — Any local command line crash/hang**.

## Likelihood Explanation
Any CKB process that defers SIGTERM handling (e.g., during a long RocksDB flush, a blocked syscall, or a custom signal handler) triggers this path. The operator runs `ckb daemon --stop`, SIGTERM is sent but not acted on within 60 seconds, and the CLI hangs. This is a realistic operational scenario requiring only local access to the node host.

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
``` [4](#0-3) 

## Proof of Concept
Mock `check_process` to always return `Ok(pid)`. Call `kill_process`. Assert the loop exits within 61 iterations and that SIGKILL was sent. With the current code, the loop never exits and the assertion fails. Alternatively: start a process that installs a SIGTERM handler that does nothing (`signal(SIGTERM, SIG_IGN)`), write its PID to a pid file, and run `ckb daemon --stop` — the CLI will hang indefinitely.

### Citations

**File:** ckb-bin/src/subcommand/daemon.rs (L42-88)
```rust
fn kill_process(pid_file: &PathBuf, name: &str) -> Result<(), ExitCode> {
    if check_process(pid_file).is_err() {
        eprintln!("{} is not running", name);
        return Ok(());
    }
    let pid_str = fs::read_to_string(pid_file).map_err(|_| ExitCode::Failure)?;
    let pid = pid_str
        .trim()
        .parse::<i32>()
        .map_err(|_| ExitCode::Failure)?;
    eprintln!(
        "stopping {} daemon service with pid {} ...",
        name,
        pid.to_string().red()
    );
    // Send a SIGTERM signal to the process
    let _ = kill(Pid::from_raw(pid), Some(Signal::SIGTERM)).map_err(|_| ExitCode::Failure);
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
    eprintln!("\n{}", "ckb daemon service stopped successfully".green());
    Ok(())
```
