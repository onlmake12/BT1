### Title
TOCTOU Race: P2P Network Secret Key Written World-Readable Before Permissions Are Restricted - (File: `util/app-config/src/configs/network.rs`)

---

### Summary

`write_secret_to_file` creates the node's secp256k1 P2P network secret key file using the process's default umask-based permissions (typically `0o644`, world-readable), writes the raw 32-byte private key, and only **after** the write completes does it call `set_permissions(0o400)`. This creates a TOCTOU (Time-of-Check-Time-of-Use) race window during which any local user can read the secret key. The correct pattern — setting the restrictive mode atomically at `open()` time — is already used in the sibling `create_tor_secret_key` function but is absent here.

---

### Finding Description

`write_secret_to_file` in `util/app-config/src/configs/network.rs` opens the secret key file with no explicit mode:

```rust
pub fn write_secret_to_file(secret: &[u8], path: PathBuf) -> Result<(), Error> {
    fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)           // ← created with umask-derived permissions (e.g. 0o644)
        .and_then(|mut file| {
            file.write_all(secret)?;   // ← secret key written while file is world-readable
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.set_permissions(fs::Permissions::from_mode(0o400))  // ← restricted only after write
            }
            ...
        })
}
``` [1](#0-0) 

On a system with a permissive umask (e.g., `0o022` → file created as `0o644`; `0o000` → `0o666`), the file is readable by all local users from the moment of creation until `set_permissions` completes. The 32-byte raw secp256k1 private key is written to disk during this window.

By contrast, `create_tor_secret_key` in `util/onion/src/onion_service.rs` correctly sets the mode atomically at `open()` time:

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o600);
}
let mut file = options.open(&onion_private_key_path)...
``` [2](#0-1) 

This inconsistency means the P2P `secret_key` file is unprotected during creation, while the Tor onion key is not.

`write_secret_to_file` is called from two paths:

1. `Config::write_secret_key_to_file` → `Config::fetch_private_key` — invoked automatically on first node startup when no key file exists.
2. `Setup::generate` — invoked by `ckb peer-id gen --secret-path <path>`. [3](#0-2) [4](#0-3) 

---

### Impact Explanation

The `secret_key` file holds the raw secp256k1 private key used for the node's P2P identity (secio). If a local attacker reads this key during the creation window:

- They can impersonate the node's P2P identity on the network.
- They can perform targeted eclipse or man-in-the-middle attacks against the node's peers by presenting the stolen identity.
- They can disrupt the node's participation in the sync, relay, and discovery protocols.

`read_secret_key` already warns when permissions are too broad, confirming the developers treat this file as sensitive: [5](#0-4) 

---

### Likelihood Explanation

- Requires local access to the same machine — the same precondition as the reference report.
- The race window is brief (microseconds to milliseconds) but is reliably exploitable with `inotifywait` or a polling loop watching the network data directory.
- The vulnerability is triggered on every first-run node initialization or every `ckb peer-id gen` invocation.
- A permissive umask (`0o022` or looser) is the default on most Linux distributions, making the file `0o644` (group- and world-readable) during the window.

---

### Recommendation

**Short term:** Mirror the pattern already used in `create_tor_secret_key`: set the restrictive file mode atomically at `open()` time using `OpenOptionsExt::mode(0o600)` (or `0o400`) before any data is written.

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    opts.mode(0o600);
}
opts.open(path).and_then(|mut file| {
    file.write_all(secret)?;
    // optionally downgrade to 0o400 after write
    file.set_permissions(fs::Permissions::from_mode(0o400))
})
```

**Long term:** Audit all other `OpenOptions::new().create(true)` call sites that write sensitive data (e.g., `tx-pool/src/persisted.rs`, `network/src/peer_store/peer_store_db.rs`) and apply appropriate restrictive modes at creation time.

---

### Proof of Concept

```bash
# Terminal 1: watch for secret_key creation and immediately read it
inotifywait -m ~/.ckb/data/network/ -e create 2>/dev/null | \
  while read dir action file; do
    if [ "$file" = "secret_key" ]; then
      cat "$dir$file" | xxd   # read raw key bytes before 0o400 is set
    fi
  done

# Terminal 2: trigger key generation (first run or explicit gen)
ckb peer-id gen --secret-path ~/.ckb/data/network/secret_key
```

On a system with umask `0o022`, the file is created as `0o644`. The `inotifywait` listener fires on `IN_CREATE`, reads the file before `set_permissions(0o400)` completes, and captures the raw 32-byte secp256k1 private key. The attacker can then use this key to impersonate the node's P2P identity.

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

**File:** util/app-config/src/configs/network.rs (L303-310)
```rust
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        warn(
            file.metadata()?.permissions().mode() & 0o177 != 0,
            "less than 0o600",
        );
    }
```

**File:** util/app-config/src/configs/network.rs (L385-399)
```rust
    fn write_secret_key_to_file(&self) -> Result<(), Error> {
        let path = self.secret_key_path();
        let random_key_pair = generate_random_key();
        write_secret_to_file(&random_key_pair, path)
    }

    /// Reads the private key from file or generates one if the file does not exist.
    #[cfg(not(target_family = "wasm"))]
    pub fn fetch_private_key(&self) -> Result<secio::SecioKeyPair, Error> {
        match self.read_secret_key()? {
            Some(key) => Ok(key),
            None => {
                self.write_secret_key_to_file()?;
                Ok(self.read_secret_key()?.expect("key must exists"))
            }
```

**File:** util/onion/src/onion_service.rs (L175-186)
```rust
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options = options.mode(0o600);
    }

    let mut file = options.open(&onion_private_key_path).map_err(|err| {
        InternalErrorKind::Other.other(format!(
            "Failed to open onion private key for writing: {:?}",
            err
        ))
    })?;
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
