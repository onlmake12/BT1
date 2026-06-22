### Title
Network Secret Key Written to World-Readable File Before Permissions Are Restricted — (`File: util/app-config/src/configs/network.rs`)

---

### Summary

`write_secret_to_file` creates the node's secp256k1 P2P secret key file using default umask-derived permissions (typically `0o644`), writes the raw 32-byte private key, and only **after** the write calls `set_permissions(0o400)`. This creates a race window during which any local user can read the secret key from disk.

---

### Finding Description

In `util/app-config/src/configs/network.rs`, the function `write_secret_to_file` (lines 265–285) opens the key file with no explicit creation mode:

```rust
fs::OpenOptions::new()
    .create(true)
    .write(true)
    .truncate(true)
    .open(path)          // ← file created with umask-derived permissions (typically 0o644)
    .and_then(|mut file| {
        file.write_all(secret)?;   // ← secret written while file is world-readable
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(fs::Permissions::from_mode(0o400))  // ← too late
        }
    })
``` [1](#0-0) 

Because no `mode()` is set at `open()` time, the kernel creates the file with `0o666 & ~umask`. With the common default umask of `0o022`, the file is created as `0o644` — world-readable. The 32-byte raw private key is written to this world-readable file, and only then is `set_permissions(0o400)` called. Any local user who reads the file between creation and the `set_permissions` call obtains the full private key.

This is called by two production paths:

1. The `ckb peer-id gen --secret-path <path>` CLI subcommand: [2](#0-1) 

2. Automatic key generation on first node startup via `fetch_private_key()` → `write_secret_key_to_file()`: [3](#0-2) 

**Contrast with the correct pattern** already used in the same codebase: `create_tor_secret_key` in `util/onion/src/onion_service.rs` correctly applies `options.mode(0o600)` at `open()` time via `OpenOptionsExt`, so the file is **never** created with broad permissions: [4](#0-3) 

---

### Impact Explanation

The network secret key is the node's secp256k1 P2P identity key. Possession of this key allows an attacker to:

- Derive the node's `PeerID` and impersonate it on the P2P network.
- Potentially perform session-level attacks against peers that have whitelisted or trusted this specific `PeerID`.

The key is loaded at node startup via `NetworkState::from_config` → `config.fetch_private_key()`: [5](#0-4) 

---

### Likelihood Explanation

**Medium.** The attacker must be a local user on the same machine (e.g., a shared server, CI environment, or container with multiple users). The race window is small but reliably exploitable with a simple `inotifywait`-based or polling loop that reads the file the moment it appears. The vulnerability is triggered on every fresh node deployment and every `ckb peer-id gen` invocation. No special privileges are required beyond local filesystem read access.

---

### Recommendation

Apply `OpenOptionsExt::mode(0o600)` (or `0o400`) at file creation time, exactly as `create_tor_secret_key` already does. This ensures the file is never created with world-readable permissions:

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o600);
}
```

Replace the current post-write `set_permissions` call with this at-creation mode. The `set_permissions(0o400)` call can remain as a belt-and-suspenders hardening step after the write.

---

### Proof of Concept

On a multi-user Linux system with default umask `0o022`:

```bash
# Terminal 1 (attacker): poll for the key file
while true; do
    cat /path/to/ckb/data/network/secret_key 2>/dev/null && break
    sleep 0.001
done | xxd
```

```bash
# Terminal 2 (victim): start ckb node (or run peer-id gen)
ckb run -C /path/to/ckb/data
```

Between the `open()` call (which creates the file as `0o644`) and the `set_permissions(0o400)` call, the attacker's loop reads the 32-byte raw secp256k1 private key. Confirmed by `strace`:

```
openat(AT_FDCWD, ".../network/secret_key",
    O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 5
write(5, "\xab\xcd...", 32)             = 32
fchmod(5, 0400)                         = 0
```

The key is readable at `0o644` from the `openat` call until `fchmod` completes.

### Citations

**File:** util/app-config/src/configs/network.rs (L265-284)
```rust
pub fn write_secret_to_file(secret: &[u8], path: PathBuf) -> Result<(), Error> {
    fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
        .and_then(|mut file| {
            file.write_all(secret)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.set_permissions(fs::Permissions::from_mode(0o400))
            }
            #[cfg(not(unix))]
            {
                let mut permissions = file.metadata()?.permissions();
                permissions.set_readonly(true);
                file.set_permissions(permissions)
            }
        })
```

**File:** util/app-config/src/configs/network.rs (L384-389)
```rust
    #[cfg(not(target_family = "wasm"))]
    fn write_secret_key_to_file(&self) -> Result<(), Error> {
        let path = self.secret_key_path();
        let random_key_pair = generate_random_key();
        write_secret_to_file(&random_key_pair, path)
    }
```

**File:** ckb-bin/src/setup.rs (L537-542)
```rust
    pub fn generate(matches: &ArgMatches) -> Result<(), ExitCode> {
        let path = matches
            .get_one::<String>(cli::ARG_SECRET_PATH)
            .expect("required on command line");
        write_secret_to_file(&generate_random_key(), path.into()).map_err(|_| ExitCode::IO)
    }
```

**File:** util/onion/src/onion_service.rs (L175-179)
```rust
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options = options.mode(0o600);
    }
```

**File:** network/src/network.rs (L99-100)
```rust
        let local_private_key = config.fetch_private_key()?;
        let local_peer_id = local_private_key.peer_id();
```
