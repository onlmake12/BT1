### Title
Plaintext Storage of P2P Network Identity Secret Key on Disk — (`util/app-config/src/configs/network.rs`)

---

### Summary

The CKB node writes its secp256k1 P2P network identity secret key to disk as raw unencrypted bytes. File-system permissions are the sole protection. No passphrase-based encryption or any other cryptographic protection is applied to the stored key material. An attacker who gains read access to the key file — through a permissive umask window at creation time, a backup, a co-located process running as the same OS user, or a misconfigured data directory — can extract the 32-byte raw private key and permanently impersonate the node on the P2P network.

---

### Finding Description

`write_secret_to_file` in `util/app-config/src/configs/network.rs` is the sole function responsible for persisting the node's P2P identity key:

```rust
pub fn write_secret_to_file(secret: &[u8], path: PathBuf) -> Result<(), Error> {
    fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
        .and_then(|mut file| {
            file.write_all(secret)?;          // raw 32-byte key written here
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.set_permissions(fs::Permissions::from_mode(0o400))
            }
            ...
        })
}
``` [1](#0-0) 

Two distinct problems exist here:

**1. No encryption of key material.** The 32-byte secp256k1 private key is written as raw bytes with no passphrase-based encryption (e.g., AES-GCM with a user-supplied password, or a KDF-derived key). Anyone who reads the file obtains a directly usable private key.

**2. TOCTOU window during file creation.** `fs::OpenOptions::new().create(true)` creates the file using the process's inherited `umask`. With a common umask of `0o022`, the file is initially created as mode `0o644` (world-readable). The `set_permissions(0o400)` call happens only *after* `write_all` completes. During that window — however brief — the raw key bytes are world-readable on disk. [2](#0-1) 

The key is loaded back via `read_secret_key`, which only *warns* (does not abort) if permissions are too permissive:

```rust
warn(
    file.metadata()?.permissions().mode() & 0o177 != 0,
    "less than 0o600",
);
``` [3](#0-2) 

The key is auto-generated and persisted on first startup via `fetch_private_key` → `write_secret_key_to_file`: [4](#0-3) 

The stored path is `<data_dir>/network/secret_key`: [5](#0-4) 

On non-Unix platforms the protection is even weaker — only a "readonly" flag is set, which does not restrict other users from reading the file: [6](#0-5) 

---

### Impact Explanation

The P2P secret key is the node's long-term cryptographic identity used by the `secio` (Secure I/O) layer for all peer-to-peer connections. Possession of the raw key allows an attacker to:

- **Impersonate the node** on the P2P network, establishing authenticated secio sessions as if they were the legitimate node.
- **Disrupt peer relationships**: peers that have whitelisted or trusted the node's peer ID will accept connections from the impersonator.
- **Perform targeted MITM** against peers that specifically dial the compromised node's peer ID.

The `Privkey` type in `util/crypto/src/secp/privkey.rs` does implement `zeroize` on `Drop` to clear key material from memory, but this in-memory protection is irrelevant once the key is persisted to disk in plaintext. [7](#0-6) 

---

### Likelihood Explanation

**Impact: 4 | Likelihood: 2**

The TOCTOU window is real but narrow. The more persistent risk is that the key file, once written, is never re-encrypted. Realistic exposure paths include:

- Automated backups (e.g., rsync, cloud snapshots) that copy the data directory before permissions are hardened.
- Deployment in containers or VMs where the data volume is shared or snapshotted.
- A co-located process running as the same OS user (e.g., a compromised plugin or monitoring agent).
- Non-Unix deployments where the "readonly" flag provides no multi-user isolation.

These are realistic operational scenarios for a production CKB node operator.

---

### Recommendation

1. **Encrypt the key at rest.** Wrap the raw key bytes with a passphrase-derived encryption key (e.g., Argon2id KDF → AES-256-GCM). Prompt the operator for the passphrase at startup, or support an environment-variable/keyring-backed unlock mechanism.

2. **Fix the TOCTOU window.** On Unix, open the file with `O_CREAT | O_WRONLY` *and* set the mode atomically via `OpenOptionsExt::mode(0o400)` *before* writing, so the file is never world-readable at any point:

```rust
#[cfg(unix)]
{
    use std::os::unix::fs::OpenOptionsExt;
    options = options.mode(0o400);
}
let mut file = options.open(&path)?;
file.write_all(secret)?;
```

3. **Abort on bad permissions.** `read_secret_key` should return an error (not just a warning) when the file's permissions are too permissive, preventing startup with an exposed key.

---

### Proof of Concept

```
# 1. Start CKB node (first run generates the key)
$ ckb run --config-dir ~/.ckb

# 2. Immediately after startup, before permissions are hardened (TOCTOU),
#    or with a permissive umask (e.g., umask 0022):
$ cat ~/.ckb/data/network/secret_key | xxd | head
# → raw 32-byte secp256k1 private key printed in hex, no decryption needed

# 3. Use the extracted key to construct a SecioKeyPair and connect to peers
#    as the legitimate node identity, impersonating it on the P2P network.
```

The `write_secret_to_file` call in `fetch_private_key` is the root cause: [8](#0-7)

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

**File:** util/app-config/src/configs/network.rs (L324-329)
```rust
    /// Gets the network secret key path.
    pub fn secret_key_path(&self) -> PathBuf {
        let mut path = self.path.clone();
        path.push("secret_key");
        path
    }
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

**File:** util/app-config/src/configs/network.rs (L391-401)
```rust
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

**File:** util/crypto/src/secp/privkey.rs (L51-56)
```rust
    pub(crate) fn zeroize(&mut self) {
        for elem in self.inner.0.iter_mut() {
            volatile_write(elem, Default::default());
            atomic_fence();
        }
    }
```
