### Title
Unbounded `read_to_end` in `read_secret_key` Causes OOM-Kill on Operator-Supplied `/dev/zero` Path — (`util/app-config/src/configs/network.rs`)

### Summary

The `ckb peer-id from-secret` subcommand accepts an operator-supplied `--secret-path` argument and passes it directly to `read_secret_key`, which calls `file.read_to_end(&mut buf)` with no size cap. Supplying `/dev/zero` causes unbounded heap allocation until the OS OOM-kills the process, violating the invariant that CLI subcommands must terminate gracefully on any operator-supplied path.

### Finding Description

`Setup::peer_id` in `ckb-bin/src/setup.rs` reads the `--secret-path` CLI argument and calls `read_secret_key(path.into())` with no validation of the path or any pre-read size check: [1](#0-0) 

`read_secret_key` in `util/app-config/src/configs/network.rs` opens the file and calls `file.read_to_end(&mut buf)` into an unbounded `Vec<u8>` with no maximum size enforced: [2](#0-1) 

A secp256k1 raw key is exactly 32 bytes. The function never checks the file size before reading, never limits the read to any reasonable bound (e.g., 64 bytes), and never returns an error for oversized input before exhausting memory. On `/dev/zero`, the kernel will satisfy every `read` call with zeroes indefinitely, causing the `Vec` to grow without bound until virtual memory is exhausted.

### Impact Explanation

The process is OOM-killed (SIGKILL) rather than exiting with a non-zero `ExitCode`. Under a memory-limited cgroup or `ulimit -v`, this is immediately reproducible. On an unconstrained system, the process will consume all available virtual memory, potentially triggering system-wide memory pressure and OOM-killing unrelated processes. The CLI subcommand does not terminate gracefully.

### Likelihood Explanation

Any unprivileged local user who can execute the `ckb` binary can trigger this with a single command. No special privileges, leaked keys, or network access are required. The path `/dev/zero` is universally readable on Linux/macOS. The scope explicitly lists CLI inputs as a valid attack surface and includes "any local command line crash" at 0–500 points.

### Recommendation

Add a size guard before or during the read. Since a secp256k1 raw key is 32 bytes, reject any file whose size exceeds a small bound (e.g., 128 bytes) before calling `read_to_end`, or replace `read_to_end` with a fixed-size read:

```rust
// In read_secret_key, replace the unbounded read:
let mut buf = [0u8; 32];
let n = file.read(&mut buf)?;
if n != 32 {
    return Err(Error::new(ErrorKind::InvalidData, "invalid secret key data"));
}
secio::SecioKeyPair::secp256k1_raw_key(&buf)
    .map(Some)
    .map_err(|_| Error::new(ErrorKind::InvalidData, "invalid secret key data"))
```

### Proof of Concept

```sh
# Under a memory-limited cgroup (e.g., 256 MB):
systemd-run --scope -p MemoryMax=256M \
    ckb peer-id from-secret --secret-path /dev/zero
# Expected (current): process is SIGKILL'd / OOM-killed
# Expected (fixed):   process exits with non-zero ExitCode and error message
```

The root cause is at: [3](#0-2) 
called unconditionally from: [4](#0-3)

### Citations

**File:** ckb-bin/src/setup.rs (L523-534)
```rust
    pub fn peer_id(matches: &ArgMatches) -> Result<PeerIDArgs, ExitCode> {
        let path = matches
            .get_one::<String>(cli::ARG_SECRET_PATH)
            .expect("required on command line");
        match read_secret_key(path.into()) {
            Ok(Some(key)) => Ok(PeerIDArgs {
                peer_id: key.peer_id(),
            }),
            Err(_) => Err(ExitCode::Failure),
            Ok(None) => Err(ExitCode::IO),
        }
    }
```

**File:** util/app-config/src/configs/network.rs (L315-320)
```rust
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).and_then(|_read_size| {
        secio::SecioKeyPair::secp256k1_raw_key(&buf)
            .map(Some)
            .map_err(|_| Error::new(ErrorKind::InvalidData, "invalid secret key data"))
    })
```
