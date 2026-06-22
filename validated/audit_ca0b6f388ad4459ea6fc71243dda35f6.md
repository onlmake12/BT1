[1](#0-0) [2](#0-1)

### Citations

**File:** ckb-bin/src/subcommand/daemon.rs (L28-40)
```rust
pub fn check_process(pid_file: &PathBuf) -> Result<i32, ExitCode> {
    let pid_str = fs::read_to_string(pid_file).map_err(|_| ExitCode::Failure)?;
    let pid = pid_str
        .trim()
        .parse::<i32>()
        .map_err(|_| ExitCode::Failure)?;

    // Check if the process is running
    match kill(Pid::from_raw(pid), None) {
        Ok(_) => Ok(pid),
        Err(_) => Err(ExitCode::Failure),
    }
}
```

**File:** ckb-bin/src/lib.rs (L122-125)
```rust
    if check_process(&pid_file).is_ok() {
        eprintln!("{}", "ckb is already running".red());
        return Ok(());
    }
```
