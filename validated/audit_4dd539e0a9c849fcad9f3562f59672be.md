### Title
Secret Key Written to World-Readable File Before Permissions Are Restricted (TOCTOU) - (File: `util/app-config/src/configs/network.rs`)

---

### Summary

`write_secret_to_file` in `util/app-config/src/configs/network.rs` creates a file with default umask permissions, writes the node's secp256k1 P2P secret key into it, and only then restricts permissions to `0o400`. This is a classic TOCTOU (time-of-check to time-of-use) race condition. A local unprivileged attacker can steal the secret key by reading the file in the window between the write and the `set_permissions` call, or by pre-creating the file at the expected path before CKB writes to it.

---

### Finding Description

`write_secret_to_file` opens the target file using `fs::OpenOptions::new().create(true).write(true).truncate(true).open(path)` with no initial mode argument. On Unix, this creates the file with the process's default umask permissions (typically `0o644`, world-readable). The 32-byte secp256k1 raw secret key is then written to this world-readable file. Only after the write completes does the code call `file.set_permissions(fs::Permissions::from_mode(0o400))` to restrict access. [1](#0-0) 

This creates two exploitable windows:

1. **Race to read:** Between `file.write_all(secret)` (line 272) and `file.set_permissions(...)` (line 276), the file exists on disk with the secret key and world-readable permissions. Any local process can open and read it.

2. **Pre-creation attack:** Because `create(true)` is used without `O_EXCL`, an attacker can pre-create the file at the path returned by `secret_key_path()` (e.g., `<data_dir>/network/secret_key`) with permissions that allow the attacker to read it after CKB writes the key. CKB will open the attacker-owned file, write the secret key into it, and the attacker retains read access.

By contrast, `create_tor_secret_key` in `util/onion/src/onion_service.rs` correctly uses `OpenOptionsExt::mode(0o600)` at file creation time, atomically setting permissions before any data is written. [2](#0-1) 

The vulnerable `write_secret_to_file` is called from `write_secret_key_to_file`, which is invoked by `fetch_private_key` whenever no existing key file is found. [3](#0-2) 

---

### Impact Explanation

The file written is the node's secp256k1 P2P network identity key, used by the `secio` protocol to authenticate all P2P connections. Theft of this key allows a local attacker to:

- Impersonate the CKB node's P2P identity on the network.
- Perform man-in-the-middle attacks on the node's encrypted P2P sessions.
- Deanonymize the node's peer relationships and network topology.

This directly affects the integrity of the node's P2P layer, which is within the CKB bounty scope (P2P/network identity).

---

### Likelihood Explanation

The attack requires only an unprivileged local account on the same machine as the CKB node — a realistic scenario for shared hosting, VPS environments, or any multi-user system. The race window (between `write_all` and `set_permissions`) is small but reliably exploitable with a tight polling loop (`inotify`/`kqueue` or busy-polling on the path). The pre-creation attack (Exploit Scenario 2) requires no race at all and is deterministic. The key file is written once at node initialization, giving the attacker a predictable, one-time opportunity.

---

### Recommendation

- **Short term:** Replace `fs::OpenOptions` with `std::fs::File::from(std::os::unix::fs::OpenOptionsExt::mode(...))` using `O_CREAT | O_EXCL | O_WRONLY` and mode `0o400` so the file is created with restricted permissions atomically before any data is written. Mirror the pattern already used correctly in `create_tor_secret_key`.
- **Long term:** Centralize all sensitive-file creation through a single utility function that enforces `O_EXCL` and sets the mode at creation time, preventing future regressions.

---

### Proof of Concept

```
# Attacker pre-creates the file before CKB initializes
mkdir -p ~/.ckb/network
touch ~/.ckb/network/secret_key
chmod 644 ~/.ckb/network/secret_key

# CKB starts, fetch_private_key() finds no key, calls write_secret_key_to_file()
# write_secret_to_file() opens the attacker-owned file (create(true) does not fail),
# writes the 32-byte secp256k1 key, then sets permissions to 0o400.
# But the attacker already has an open fd or reads it before chmod completes:

inotifywait -e close_write ~/.ckb/network/secret_key && \
  cat ~/.ckb/network/secret_key | xxd
# => 32 bytes of raw secp256k1 secret key printed to attacker's terminal
```

### Citations

**File:** util/app-config/src/configs/network.rs (L265-285)
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
}
```

**File:** util/app-config/src/configs/network.rs (L385-400)
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
