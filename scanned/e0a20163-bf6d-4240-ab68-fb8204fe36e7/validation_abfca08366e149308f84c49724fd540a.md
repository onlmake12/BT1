### Title
Onion Service Private Key Loaded Without File Permission Check — (`util/onion/src/onion_service.rs`)

---

### Summary

`load_tor_secret_key()` in `util/onion/src/onion_service.rs` reads the Tor onion service v3 private key from disk using a plain `std::fs::read_to_string` call with no file-permission check of any kind. If the key file has overly permissive permissions (e.g., world-readable due to a Docker volume mount, backup script, or operator error), the node silently loads and uses the key without warning. The analogous P2P network secret key loader (`read_secret_key`) at least emits a log warning on bad permissions; the onion key loader does neither.

---

### Finding Description

**Creation path** (`create_tor_secret_key`, lines 163–196) correctly sets `mode(0o600)` on Unix when the key file is first generated:

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o600);
}
``` [1](#0-0) 

**Load path** (`load_tor_secret_key`, lines 201–223) performs no permission check at all — it calls `std::fs::read_to_string` directly:

```rust
fn load_tor_secret_key(onion_private_key_path: String) -> Result<TorSecretKeyV3, Error> {
    let raw = base64::engine::general_purpose::STANDARD
        .decode(
            std::fs::read_to_string(&onion_private_key_path)...
``` [2](#0-1) 

This is called unconditionally from `load_or_create_tor_secret_key` whenever the file already exists:

```rust
let key = match is_onion_private_key_exists {
    true => load_tor_secret_key(onion_private_key_path)?,
    false => create_tor_secret_key(onion_private_key_path)?,
};
``` [3](#0-2) 

**Contrast with the P2P network key loader** (`read_secret_key`), which at minimum emits a `warn!` log if Unix permissions are more permissive than `0o600`:

```rust
warn(
    file.metadata()?.permissions().mode() & 0o177 != 0,
    "less than 0o600",
);
``` [4](#0-3) 

The onion key loader has no equivalent check — not even a warning.

The `OnionService::new()` entry point is called from the launcher during node startup whenever the Tor onion feature is configured: [5](#0-4) 

---

### Impact Explanation

The Tor onion service v3 private key (`onion_private_key`) is the long-term identity key for the node's hidden service address. Leaking it allows any party to:

1. Impersonate the node's `.onion` address on the Tor network, redirecting peers that connect to it.
2. Deanonymize the node operator by correlating the onion address with other network activity.

If the key file's permissions are widened (e.g., `chmod 644`, a Docker bind-mount with default umask, a backup tool that strips permissions, or a misconfigured deployment), any local user or process on the same host can read the raw key bytes. The node provides no warning and continues operating normally, leaving the operator unaware of the exposure.

**Impact: Medium** — loss of the onion identity key enables peer impersonation and operator deanonymization for nodes using the Tor hidden-service feature.

---

### Likelihood Explanation

The file is created with correct `0o600` permissions, so a fresh deployment is safe. However, permissions can silently change through:

- Docker volume mounts (default umask may not preserve `0o600`)
- Filesystem backup/restore tools that do not preserve permissions
- Operator `chmod` or `cp` commands
- Automated deployment scripts

Because the node emits no warning when it loads a world-readable key file, the operator has no signal that the key is exposed. The P2P secret key at least logs a warning; the onion key does not.

**Likelihood: Low-Medium** — requires a secondary permission-widening event, but the complete absence of any check (versus the warn-only approach used elsewhere) makes silent exploitation plausible in real deployments.

---

### Recommendation

Add a permission check inside `load_tor_secret_key` mirroring the pattern already used in `read_secret_key`. At minimum, emit a `warn!` log. Ideally, refuse to start (return an error) if the file is readable by group or other:

```rust
fn load_tor_secret_key(onion_private_key_path: String) -> Result<TorSecretKeyV3, Error> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let meta = std::fs::metadata(&onion_private_key_path).map_err(|e| {
            InternalErrorKind::Other.other(format!("stat onion key failed: {}", e))
        })?;
        if meta.permissions().mode() & 0o177 != 0 {
            return Err(InternalErrorKind::Other
                .other("onion_private_key file permissions are too permissive; expected 0o600 or stricter")
                .into());
        }
    }
    // ... existing read logic
}
```

Additionally, consider upgrading the `read_secret_key` warning to a hard error for consistency.

---

### Proof of Concept

1. Start a CKB node with the Tor onion service enabled. The key is created at `<network_path>/onion_private_key` with `0o600`.
2. Run `chmod 644 <network_path>/onion_private_key` (simulating a backup restore or Docker volume mount).
3. Restart the node. `load_tor_secret_key` calls `std::fs::read_to_string` with no permission check and loads the key silently — no warning is logged.
4. Any local user (`ls -la` confirms world-readable) can now `cat <network_path>/onion_private_key` and obtain the raw base64-encoded 64-byte Ed25519 private key, enabling full impersonation of the node's onion address. [6](#0-5)

### Citations

**File:** util/onion/src/onion_service.rs (L27-33)
```rust
    pub fn new(
        handle: Handle,
        config: OnionServiceConfig,
        node_id: String,
    ) -> Result<(OnionService, Multiaddr), Error> {
        let key: TorSecretKeyV3 =
            load_or_create_tor_secret_key(config.onion_private_key_path.clone())?;
```

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

**File:** util/onion/src/onion_service.rs (L175-179)
```rust
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options = options.mode(0o600);
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
