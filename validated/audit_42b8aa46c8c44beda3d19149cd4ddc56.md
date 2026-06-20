### Title
Network Secret Key Written to Disk Before File Permissions Are Restricted — (`File: util/app-config/src/configs/network.rs`)

---

### Summary

`write_secret_to_file` creates the node's P2P identity secret key file with default umask permissions (typically `0o644`, world-readable), writes the raw 32-byte secp256k1 private key into it, and only then restricts permissions to `0o400`. Any local unprivileged user on the same host can race to read the plaintext key during this window. The contrast with `create_tor_secret_key` — which correctly sets `mode(0o600)` atomically at file creation — confirms this is an unintentional inconsistency.

---

### Finding Description

`write_secret_to_file` in `util/app-config/src/configs/network.rs` opens the secret key file using `fs::OpenOptions` without specifying a creation mode:

```rust
fs::OpenOptions::new()
    .create(true)
    .write(true)
    .truncate(true)
    .open(path)                          // ← no .mode() — inherits umask (e.g. 0o644)
    .and_then(|mut file| {
        file.write_all(secret)?;         // ← raw key bytes written while world-readable
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(fs::Permissions::from_mode(0o400))  // ← restricted only after
        }
    })
``` [1](#0-0) 

The file is created with whatever permissions the process umask allows (commonly `0o644`), the 32-byte raw secret key is written, and only afterward are permissions tightened to `0o400`. Between file creation and the `set_permissions` call there is a window — however brief — during which any local user can `open()` and `read()` the file.

Compare this with `create_tor_secret_key` in `util/onion/src/onion_service.rs`, which correctly sets the mode atomically at creation time:

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o600);   // ← mode set before open(), no window
}
let mut file = options.open(&onion_private_key_path)...
file.write_all(...)...
``` [2](#0-1) 

The inconsistency is clear: the Tor onion key is protected correctly; the P2P network identity key is not.

`write_secret_to_file` is called in two places:

1. **Auto-generation at node startup** via `Config::write_secret_key_to_file` → `Config::fetch_private_key`, invoked from `NetworkState::from_config` every time a node starts without an existing key file. [3](#0-2) 

2. **CLI `peer-id gen` subcommand** via `PeerIDArgs::generate`, which calls `write_secret_to_file` directly with a user-supplied path. [4](#0-3) 

In both cases the raw key bytes are transiently world-readable.

---

### Impact Explanation

The secret key stored at `data/network/secret_key` is the node's secp256k1 P2P identity key. It is loaded by `NetworkState::from_config` and used for all secio-encrypted P2P sessions. [5](#0-4) 

An attacker who obtains this key can:
- **Impersonate the node** on the CKB P2P network, accepting connections under the victim node's peer ID.
- **Decrypt or MITM past and future secio sessions** established with that identity, allowing manipulation of block/transaction relay data received by the node.
- **Deanonymize the node operator** by correlating the peer ID across network observations.

---

### Likelihood Explanation

The window exists every time the key file is first generated (new node setup or after `ckb reset-data --network`). On a shared hosting environment (VPS, container with a shared OS user namespace, or a misconfigured multi-tenant server), a co-resident unprivileged process can use `inotify` or a tight polling loop on the parent directory to detect file creation and immediately `open()` the file before `set_permissions` executes. This is a standard local TOCTOU race and requires no special privileges. The attack is fully automatable.

---

### Recommendation

**Short term:** Set the file mode atomically at creation time using `OpenOptionsExt::mode`, mirroring the pattern already used in `create_tor_secret_key`:

```rust
use std::os::unix::fs::OpenOptionsExt;
fs::OpenOptions::new()
    .create(true)
    .write(true)
    .truncate(true)
    .mode(0o600)          // ← restrict before any data is written
    .open(path)
    .and_then(|mut file| {
        file.write_all(secret)
        // no set_permissions needed; mode was set atomically
    })
```

**Long term:** Audit all other locations where sensitive material is written to disk to confirm they all set restrictive modes at creation time, not after the fact.

---

### Proof of Concept

```bash
# Terminal 1 — attacker (unprivileged local user)
inotifywait -m -e create /path/to/ckb/data/network/ |
while read dir event file; do
    cat "/path/to/ckb/data/network/$file" 2>/dev/null | xxd
done

# Terminal 2 — victim
ckb run   # or: ckb peer-id gen --secret-path /path/to/ckb/data/network/secret_key
```

During the window between `open()` and `set_permissions(0o400)`, Terminal 1 captures the 32-byte raw secp256k1 private key. The attacker can then reconstruct the `SecioKeyPair` and impersonate the node.

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

**File:** util/app-config/src/configs/network.rs (L384-401)
```rust
    #[cfg(not(target_family = "wasm"))]
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
    }
```

**File:** util/onion/src/onion_service.rs (L170-194)
```rust
    #[cfg_attr(not(unix), allow(unused_mut))]
    let mut file_options = OpenOptions::new();
    #[cfg_attr(not(unix), allow(unused_mut))]
    let mut options = file_options.create(true).truncate(true).write(true);

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
    file.write_all(
        &base64::engine::general_purpose::STANDARD
            .encode(key.as_bytes())
            .into_bytes(),
    )
    .map_err(|err| {
        InternalErrorKind::Other.other(format!("Failed to write onion private key: {:?}", err))
    })?;
```

**File:** ckb-bin/src/setup.rs (L536-542)
```rust
    /// Generates the network secret key.
    pub fn generate(matches: &ArgMatches) -> Result<(), ExitCode> {
        let path = matches
            .get_one::<String>(cli::ARG_SECRET_PATH)
            .expect("required on command line");
        write_secret_to_file(&generate_random_key(), path.into()).map_err(|_| ExitCode::IO)
    }
```

**File:** network/src/network.rs (L97-100)
```rust
    pub fn from_config(config: NetworkConfig) -> Result<NetworkState, Error> {
        config.create_dir_if_not_exists()?;
        let local_private_key = config.fetch_private_key()?;
        let local_peer_id = local_private_key.peer_id();
```
