The code is confirmed. The logic bug is real.

In Rust, `match` arms are evaluated in order. The first arm `Ok(_)` at line 64 matches unconditionally whenever `check_process` returns `Ok` (process still alive). The second arm `_ if wait_time <= 0` at line 70 is a wildcard with a guard — but it is only reached if the first arm does **not** match, i.e., when `check_process` returns `Err`. When the process is still alive, `wait_time` decrements to zero and below, but the guard is never evaluated. The force-kill path at line 79 is unreachable in the scenario it was designed for.

---

Audit Report

## Title
Infinite loop in `kill_process` when process ignores SIGTERM — (`ckb-bin/src/subcommand/daemon.rs`)

## Summary
The `kill_process` function sends SIGTERM and then polls `check_process` in a loop, decrementing `wait_time` and intending to escalate to SIGKILL after 60 seconds. Due to a misplaced match arm, the `_ if wait_time <= 0` guard is only reachable when `check_process` returns `Err` (process already gone), not when it returns `Ok` (process still alive). If the target process never terminates after SIGTERM, the loop runs forever and `ckb daemon --stop` hangs indefinitely.

## Finding Description [1](#0-0) 

Rust evaluates `match` arms in order. The arm `Ok(_)` at line 64 matches unconditionally whenever `check_process` returns `Ok`. The arm `_ if wait_time <= 0` at line 70 is a wildcard with a guard — it is only reached when the first arm does not match, i.e., when `check_process` returns `Err`. As long as the process is alive, `check_process` returns `Ok`, the first arm fires, `wait_time` decrements past zero, and the guard at line 70 is never evaluated. The SIGKILL path at line 79 is dead code in the scenario it was designed for.

## Impact Explanation
`ckb daemon --stop` hangs indefinitely. The operator loses the ability to stop the node through the normal CLI path. This is a local command-line hang, matching the allowed CKB bounty impact: **Note (0–500 points) — Any local command line crash/hang**.

## Likelihood Explanation
Any CKB process that defers or ignores SIGTERM (e.g., during a long flush, a blocked syscall, or a custom signal handler) triggers this path. No external attacker is required; the condition arises from normal operational scenarios such as a node under heavy I/O load.

## Recommendation
Move the timeout check into the `Ok` arm:

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
Mock `check_process` to always return `Ok(pid)`. Call `kill_process`. Assert the loop exits after at most 60 iterations and that SIGKILL is sent. With the current code, the assertion fails — the loop never exits because `wait_time` goes negative while the `Ok(_)` arm continues to match unconditionally.

### Citations

**File:** ckb-bin/src/subcommand/daemon.rs (L61-86)
```rust
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
