### Title
Onion Private Key Stored Without File Access Control on Non-Unix Platforms — (File: `util/onion/src/onion_service.rs`)

### Summary
`create_tor_secret_key` in `util/onion/src/onion_service.rs` writes the Tor v3 hidden-service private key to disk without applying any file-permission restriction on non-Unix (Windows) platforms, and with a weaker-than-necessary mode (`0o600` instead of `0o400`) on Unix. Additionally, `load_tor_secret_key` never checks file permissions when reading the key back, unlike the analogous `read_secret_key` function for the P2P network secret key, which at least emits a warning. This is a direct structural analog to the reported Pontem keychain issue: sensitive key material is persisted without specifying the appropriate access-control level.

### Finding Description

**`create_tor_secret_key`** (`util/onion/src/onion_service.rs`, lines 163–197):

```rust
#[cfg_attr(not(unix), allow(unused_mut))]
let mut file_options = OpenOptions::new();
#[cfg_attr(not(unix), allow(unused_mut))]
let mut options = file_options.create(true).truncate(true).write(true);

#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o600);   // rw------- on Unix
}
// ← No #[cfg(not(unix))] branch: file is created with OS-default
//   permissions on Windows (typically world-readable).
``` [1](#0-0) 

Compare with `write_secret_to_file` for the P2P network secret key (`util/app-config/src/configs/network.rs`, lines 264–285), which:
- Sets `0o400` (read-only for owner) on Unix, and
- Explicitly calls `set_readonly(true)` on non-Unix. [2](#0-1) 

The onion key path has neither protection on non-Unix, and uses a more permissive mode (`0o600` vs `0o400`) on Unix.

**`load_tor_secret_key`** (`util/onion/src/onion_service.rs`, lines 201–223) reads the key with `std::fs::read_to_string` and performs no permission check whatsoever. [3](#0-2) 

By contrast, `read_secret_key` (`util/app-config/src/configs/network.rs`, lines 287–321) checks `mode() & 0o177 != 0` on Unix and `!permissions.readonly()` on non-Unix, and warns the operator. [4](#0-3) 

### Impact Explanation

The `onion_private_key` file is the Tor v3 Ed25519 hidden-service key. It deterministically derives the node's `.onion` address. If an attacker obtains this key they can:

1. **Impersonate the node's onion address** — register the same hidden service on a different Tor instance, causing peers that connect to the `.onion` address to reach the attacker's node instead.
2. **Intercept or disrupt P2P traffic** — peers that have the node's onion multiaddr cached will connect to the attacker, enabling eclipse-style attacks on Tor-using CKB nodes.
3. **Deanonymize the node** — the key is the sole secret protecting the node's Tor identity; its exposure permanently links the `.onion` address to whoever holds the key.

On non-Unix platforms the file is created with OS-default permissions (world-readable on Windows with typical ACLs), so any local user or process can read it without any privilege escalation. [5](#0-4) 

### Likelihood Explanation

The feature is opt-in (`listen_on_onion: true` in `[network.onion]`), so only nodes that enable Tor hidden-service mode are affected. [6](#0-5) 

On non-Unix (Windows), the likelihood is **high** for any multi-user system or any system where other processes run under different accounts — the file is readable by default. On Unix the likelihood is lower because `0o600` restricts reads to the file owner, but the unnecessary write bit (`0o200`) means the key can be silently overwritten by the owner's own processes (e.g., a compromised dependency), and no permission warning is ever emitted on load.

### Recommendation

1. **`create_tor_secret_key`**: Add a `#[cfg(not(unix))]` branch that calls `set_readonly(true)` after writing, mirroring `write_secret_to_file`. On Unix, change the mode from `0o600` to `0o400` (read-only for owner).

```rust
// After file.write_all(...)
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o400);  // was 0o600
}
#[cfg(not(unix))]
{
    let mut perms = file.metadata()?.permissions();
    perms.set_readonly(true);
    file.set_permissions(perms)?;
}
```

2. **`load_tor_secret_key`**: Add a permission check analogous to `read_secret_key`, warning (or erroring) if the file is more permissive than expected.

### Proof of Concept

On a Windows host with CKB configured for onion mode:

```
# After node startup, the onion_private_key file is created.
# Any local user can read it:
type %APPDATA%\ckb\data\network\onion_private_key
# → base64-encoded 64-byte Ed25519 key printed to stdout with no error.
```

The attacker decodes the key, registers the same `.onion` address via a separate Tor instance, and peers that attempt to connect to the legitimate node's onion multiaddr are routed to the attacker's node instead. [7](#0-6) [2](#0-1)

### Citations

**File:** util/onion/src/onion_service.rs (L154-161)
```rust
fn load_or_create_tor_secret_key(onion_private_key_path: String) -> Result<TorSecretKeyV3, Error> {
    let is_onion_private_key_exists = Path::new(&onion_private_key_path).exists();
    let key = match is_onion_private_key_exists {
        true => load_tor_secret_key(onion_private_key_path)?,
        false => create_tor_secret_key(onion_private_key_path)?,
    };
    Ok(key)
}
```

**File:** util/onion/src/onion_service.rs (L163-197)
```rust
fn create_tor_secret_key(onion_private_key_path: String) -> Result<TorSecretKeyV3, Error> {
    let key = torut::onion::TorSecretKeyV3::generate();
    info!(
        "Generated new onion service v3 key for address: {}",
        key.public().get_onion_address()
    );

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

    Ok(key)
}
```

**File:** util/onion/src/onion_service.rs (L201-223)
```rust
fn load_tor_secret_key(onion_private_key_path: String) -> Result<TorSecretKeyV3, Error> {
    let raw = base64::engine::general_purpose::STANDARD
        .decode(
            std::fs::read_to_string(&onion_private_key_path).map_err(|err| {
                InternalErrorKind::Other.other(format!(
                    "Read onion private key({}) failed: {}",
                    onion_private_key_path, err
                ))
            })?,
        )
        .map_err(|err| {
            InternalErrorKind::Other.other(format!("Failed to decode onion private key: {:?}", err))
        })?;
    let raw = raw.as_slice();
    if raw.len() != TOR_SECRET_KEY_LENGTH {
        return Err(InternalErrorKind::Other
            .other("Invalid secret key length")
            .into());
    }
    let mut buf = [0u8; TOR_SECRET_KEY_LENGTH];
    buf.copy_from_slice(raw);
    Ok(TorSecretKeyV3::from(buf))
}
```

**File:** util/app-config/src/configs/network.rs (L131-155)
```rust
/// Onion related config options
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct OnionConfig {
    // Automatically create Tor onion service
    pub listen_on_onion: bool,
    // Tor server url: like: 127.0.0.1:9050
    pub onion_server: Option<String>,
    // The onion service will proxy incoming traffic to `p2p_listen_address`.
    // If the CKB's peer-to-peer listen address is not set to the default 127.0.0.1
    // with the port specified in `[network].listen_addresses` for IPv4, you should configure this field.
    pub p2p_listen_address: Option<String>,
    // path to store onion private key, default is ./data/network/onion_private_key
    pub onion_private_key_path: Option<String>,
    // tor controller url, example: 127.0.0.1:9051
    #[serde(default = "default_tor_controller")]
    pub tor_controller: String,
    // tor controller hashed password
    pub tor_password: Option<String>,
    // The external port that the onion service will expose. Default is 8115.
    // This is the port that will be advertised in the onion address,
    // while traffic will be forwarded to `p2p_listen_address`.
    #[serde(default = "default_onion_external_port")]
    pub onion_external_port: u16,
}
```

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

**File:** util/app-config/src/configs/network.rs (L287-314)
```rust
/// Load secret key from path
pub fn read_secret_key(path: PathBuf) -> Result<Option<secio::SecioKeyPair>, Error> {
    let mut file = match fs::File::open(path.clone()) {
        Ok(file) => file,
        Err(_) => return Ok(None),
    };
    let warn = |m: bool, d: &str| {
        if m {
            ckb_logger::warn!(
                "Your network secret file's permission is not {}, path: {:?}. \
                Please fix it as soon as possible",
                d,
                path
            )
        }
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        warn(
            file.metadata()?.permissions().mode() & 0o177 != 0,
            "less than 0o600",
        );
    }
    #[cfg(not(unix))]
    {
        warn(!file.metadata()?.permissions().readonly(), "readonly");
    }
```
