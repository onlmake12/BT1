### Title
Network P2P Secret Key Written to World-Readable File Before Permission Restriction (TOCTOU Race) - (File: `util/app-config/src/configs/network.rs`)

---

### Summary

The `write_secret_to_file` function in CKB's network configuration creates the node's secp256k1 P2P identity key file using default umask permissions (typically `0o644`, world-readable on Linux), writes the raw 32-byte secret key, and only then restricts permissions to `0o400`. This TOCTOU window allows any local user or co-resident process to read the key before it is protected.

---

### Finding Description

In `write_secret_to_file` (`util/app-config/src/configs/network.rs`, lines 265–285), the file is opened with:

```rust
fs::OpenOptions::new()
    .create(true)
    .write(true)
    .truncate(true)
    .open(path)
    .and_then(|mut file| {
        file.write_all(secret)?;          // raw key bytes written here
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(fs::Permissions::from_mode(0o400))  // restricted AFTER write
        }
``` [1](#0-0) 

No creation mode is specified in `OpenOptions`. On Linux/Unix, the kernel applies the process umask (default `0o022`) to the implicit `0o666`, yielding `0o644` — world-readable. The raw 32-byte secp256k1 secret key is written to the file while it is still world-readable. Only after `write_all` succeeds does the code call `set_permissions(0o400)`.

By contrast, the Tor onion private key creation in `create_tor_secret_key` correctly sets the mode at file-open time using `OpenOptionsExt::mode(0o600)`, preventing any world-readable window:

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o600);   // set at creation, not after write
}
``` [2](#0-1) 

The vulnerable `write_secret_to_file` is called from two production paths:

1. `Config::write_secret_key_to_file` → `Config::fetch_private_key` — invoked automatically on first node startup when no key file exists. [3](#0-2) 

2. `Setup::generate` — invoked by the `ckb peer-id gen` CLI subcommand. [4](#0-3) 

Additionally, `read_secret_key` only emits a `warn!` log when it detects incorrect permissions but still loads and uses the key regardless:

```rust
warn(
    file.metadata()?.permissions().mode() & 0o177 != 0,
    "less than 0o600",
);
``` [5](#0-4) 

This means even if the file is left world-readable (e.g., on a system with a permissive umask), the node continues operating without error.

---

### Impact Explanation

The network secret key is the node's secp256k1 P2P identity key. It is used by the `secio` protocol to authenticate the node to all peers. Compromise of this key allows an attacker to:

- **Impersonate the node's P2P identity** on the network, causing peers to believe they are connected to the legitimate node.
- **Perform targeted eclipse attacks**: by presenting the stolen identity, the attacker can intercept or manipulate the node's view of the blockchain, feeding it crafted headers or transactions.
- **Disrupt the node's network participation**: the attacker can establish connections using the stolen identity, causing the legitimate node to be rejected by peers that enforce one-connection-per-peer-id rules.

The key is stored as raw unencrypted bytes with no passphrase protection, so extraction requires only filesystem read access during the TOCTOU window. [6](#0-5) 

---

### Likelihood Explanation

The TOCTOU window occurs on every first startup of a CKB node (when no `secret_key` file exists) and on every `ckb peer-id gen` invocation. On shared Linux servers (common for node operators), other local users can race to read the file. A malicious co-resident process (e.g., a compromised dependency or a process running as the same UID) can use `inotify` to watch the network data directory and read the file the instant it is created, before `set_permissions` is called. The window is small but reliably exploitable with filesystem event monitoring. The default Linux umask of `0o022` guarantees the file is created `0o644` unless the operator has explicitly hardened their umask.

---

### Recommendation

**Short term**: Replace the two-step create-then-chmod pattern with a single atomic open using `OpenOptionsExt::mode(0o400)` (or `0o600`) at file creation time, matching the pattern already used in `create_tor_secret_key`:

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o400);
}
let mut file = options.open(path)?;
file.write_all(secret)?;
// No separate set_permissions call needed
```

**Long term**: Store the network secret key encrypted at rest (e.g., with a passphrase-derived key or a hardware security module), so that filesystem read access alone is insufficient to extract the key.

---

### Proof of Concept

1. On a Linux system with a second local user account (or a process running as the same UID), set up an `inotify` watch on the CKB network data directory (e.g., `~/.ckb/data/network/`):

```bash
inotifywait -m -e create ~/.ckb/data/network/ &
```

2. Trigger key generation by running `ckb peer-id gen --secret-path ~/.ckb/data/network/secret_key` or starting a fresh CKB node.

3. The `inotifywait` callback fires the instant the file is created. Immediately read the file:

```bash
# In the inotifywait callback:
cat ~/.ckb/data/network/secret_key | xxd
```

4. The raw 32-byte secp256k1 key is readable before `set_permissions(0o400)` is applied, because the file was created with `0o644`.

5. Use the extracted key bytes to construct a `SecioKeyPair` and connect to the CKB P2P network impersonating the victim node's peer ID. [7](#0-6)

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

**File:** util/app-config/src/configs/network.rs (L315-320)
```rust
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).and_then(|_read_size| {
        secio::SecioKeyPair::secp256k1_raw_key(&buf)
            .map(Some)
            .map_err(|_| Error::new(ErrorKind::InvalidData, "invalid secret key data"))
    })
```

**File:** util/app-config/src/configs/network.rs (L384-400)
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
```

**File:** util/onion/src/onion_service.rs (L175-179)
```rust
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options = options.mode(0o600);
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
