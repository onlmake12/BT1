### Title
Plaintext Network Secret Key Written to World-Readable File Before Permissions Are Restricted (TOCTOU) — (`File: util/app-config/src/configs/network.rs`)

---

### Summary

`write_secret_to_file` in `util/app-config/src/configs/network.rs` creates the node's Secp256k1 P2P identity key file with default filesystem permissions (typically `0o644`, world-readable), writes the raw 32-byte secret key, and only then restricts permissions to `0o400`. Any local unprivileged user on the same host can read the key during this window. The key is also stored as raw unencrypted bytes with no passphrase protection, meaning any user who gains read access to the file at any time recovers the full secret.

---

### Finding Description

`write_secret_to_file` is the sole function used to persist the node's P2P identity key:

```rust
pub fn write_secret_to_file(secret: &[u8], path: PathBuf) -> Result<(), Error> {
    fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)                          // (1) file created with umask perms (e.g. 0o644)
        .and_then(|mut file| {
            file.write_all(secret)?;         // (2) raw key bytes written
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.set_permissions(fs::Permissions::from_mode(0o400))  // (3) perms restricted
            }
        })
}
``` [1](#0-0) 

The sequence is: **create → write secret → chmod**. Between steps (1) and (3), the file exists on disk with default umask permissions (commonly `0o644` on Linux, making it world-readable). A local attacker using `inotifywait` or a polling loop on the CKB data directory can read the file during this window.

The correct approach is to open the file with restricted permissions atomically at creation time using `OpenOptionsExt::mode(0o400)` (as the Tor onion key writer does with `0o600`):

```rust
// onion_service.rs does it correctly:
options = options.mode(0o600);  // set at open() time, not after write
``` [2](#0-1) 

Additionally, `read_secret_key` only **warns** about incorrect permissions but continues loading the key regardless:

```rust
warn(
    file.metadata()?.permissions().mode() & 0o177 != 0,
    "less than 0o600",
);
``` [3](#0-2) 

This means even if the file is left world-readable (e.g., due to the race or manual misconfiguration), the node silently proceeds.

The key is stored as raw unencrypted binary bytes — no passphrase, no KDF, no encryption. Any process that reads the file at any point recovers the full 32-byte Secp256k1 private key.

This function is called both at node startup via `fetch_private_key` and via the `ckb peer-id gen` CLI subcommand: [4](#0-3) [5](#0-4) 

---

### Impact Explanation

The network secret key is the node's Secp256k1 P2P identity used by the `secio` transport layer. Theft of this key allows an attacker to:

1. **Impersonate the node** on the P2P network — the attacker can present the same peer ID and complete the secio handshake as the legitimate node.
2. **Eclipse the node** — by impersonating the node's peer ID, the attacker can intercept or redirect connections that peers initiate toward the legitimate node.
3. **Disrupt P2P connectivity** — the attacker can run a competing instance with the stolen key, causing connection conflicts and peer bans against the legitimate node.

The key is loaded into `NetworkState::local_private_key` and used for all P2P session establishment: [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The TOCTOU window is short (microseconds to milliseconds) but reliably exploitable on a shared host using filesystem event monitoring (`inotifywait -e create`). Shared cloud VMs, container environments with host-mounted volumes, or any multi-user Linux system running CKB are affected. The plaintext-at-rest aspect is always exploitable by any process running as the same user or root (e.g., after a container escape or privilege escalation). The CKB data directory path is predictable (`~/.ckb/data/network/secret_key` or the configured path).

---

### Recommendation

1. **Fix the TOCTOU**: Open the file with restricted permissions atomically at creation time using `OpenOptionsExt::mode(0o400)` (Unix) before writing any secret material — exactly as `create_tor_secret_key` does with `0o600`.
2. **Encrypt the key at rest**: Protect the stored key with a passphrase-derived key (e.g., using Argon2 + AES-GCM), prompting the operator at startup, analogous to how Bitcoin Core encrypts its wallet.
3. **Enforce permissions on load**: In `read_secret_key`, treat incorrect permissions as a hard error rather than a warning, refusing to start if the file is accessible to other users.

---

### Proof of Concept

On a shared Linux host, while CKB is being initialized for the first time (or `ckb peer-id gen` is run):

```bash
# Attacker process (runs as any local user):
inotifywait -m ~/.ckb/data/network/ -e create |
while read dir action file; do
    if [ "$file" = "secret_key" ]; then
        # Race: read before chmod 0o400 is applied
        cp "$dir$file" /tmp/stolen_key
        echo "Key stolen: $(xxd /tmp/stolen_key)"
    fi
done
```

The 32-byte raw Secp256k1 private key is captured. The attacker can then derive the peer ID and impersonate the node:

```bash
ckb peer-id from-secret --secret-path /tmp/stolen_key
# peer_id: <same peer ID as victim node>
``` [7](#0-6)

### Citations

**File:** util/app-config/src/configs/network.rs (L264-285)
```rust
/// Secret key storage
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

**File:** util/app-config/src/configs/network.rs (L383-401)
```rust
    /// Generates a random secret key and saves it into the file.
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

**File:** util/onion/src/onion_service.rs (L175-179)
```rust
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options = options.mode(0o600);
    }
```

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

**File:** network/src/network.rs (L96-100)
```rust
    #[cfg(not(target_family = "wasm"))]
    pub fn from_config(config: NetworkConfig) -> Result<NetworkState, Error> {
        config.create_dir_if_not_exists()?;
        let local_private_key = config.fetch_private_key()?;
        let local_peer_id = local_private_key.peer_id();
```
