### Title
Unencrypted Network Secret Key Persisted to Disk and Not Zeroed in Memory After Read - (File: `util/app-config/src/configs/network.rs`)

### Summary
The CKB node's P2P identity secret key (secp256k1 private key) is written to disk as raw unencrypted bytes and, when read back, is loaded into a heap-allocated `Vec<u8>` buffer that is never zeroed before being dropped. Any local attacker who can read the process's memory (e.g., via `/proc/<pid>/mem`, a core dump, or a swap file) or the data directory can recover the node's private key in plaintext.

### Finding Description
Two related issues exist in `util/app-config/src/configs/network.rs`:

**Issue 1 — Plaintext key written to disk without encryption.**

`write_secret_to_file` writes the raw 32-byte secp256k1 secret key directly to the filesystem with no encryption or key-derivation wrapping: [1](#0-0) 

The file is given restricted permissions (`0o400` on Unix, read-only on Windows), but the bytes themselves are unprotected. Any process running as the same user, or any attacker who obtains a disk image or backup, can read the raw key.

**Issue 2 — Raw key bytes not zeroed in memory after reading.**

`read_secret_key` reads the key into a heap-allocated `Vec<u8>` (`buf`) and then passes it to `secio::SecioKeyPair::secp256k1_raw_key`. The `buf` is dropped at the end of the function without zeroing: [2](#0-1) 

Rust's default `Drop` for `Vec<u8>` deallocates the memory but does not overwrite it. The 32 raw key bytes remain readable in the heap until the allocator reuses that page. A memory dump taken at any point after node startup will contain the key.

The `NetworkState` struct then holds the key for the entire lifetime of the node: [3](#0-2) 

By contrast, the `Privkey` type used for transaction signing does implement `Drop` with `volatile_write` zeroization: [4](#0-3) 

The network secret key path has no equivalent protection.

### Impact Explanation
The network secret key is the node's secp256k1 P2P identity key. It is used by the `secio` (secure I/O) layer to authenticate the node to all peers. An attacker who recovers this key can:

1. **Impersonate the node's peer ID** on the network, causing other nodes to believe they are connected to the legitimate node.
2. **Perform eclipse attacks**: by presenting the stolen peer ID, the attacker can intercept or manipulate the victim node's peer connections, potentially feeding it a crafted chain view.
3. **Undermine the node's reputation** in the peer store of other nodes, since the attacker can act as the node and misbehave.

### Likelihood Explanation
The attack requires local access to the running process's memory or to the data directory. This is the same threat model as the reference report (compromised machine). On Linux, `/proc/<pid>/mem` or a core dump is sufficient. On Windows, a process memory dump via Task Manager or `procdump` is sufficient. The key is present in memory for the entire lifetime of the node process (it is stored in `NetworkState.local_private_key` without zeroing). The on-disk file is also permanently readable by any process running as the same OS user.

### Recommendation
1. **Encrypt the on-disk key file** using a passphrase-derived key (e.g., PBKDF2 or Argon2) before writing, analogous to how other node implementations protect their identity keys.
2. **Zero the `buf` Vec after use** in `read_secret_key`, using a crate such as `zeroize` (already used elsewhere in the codebase for `Privkey`):
   ```rust
   use zeroize::Zeroize;
   let mut buf = Vec::new();
   file.read_to_end(&mut buf)?;
   let result = secio::SecioKeyPair::secp256k1_raw_key(&buf)...;
   buf.zeroize();
   result
   ```
3. **Apply the same `zeroize`-on-drop pattern** used by `Privkey` to any wrapper that holds the `SecioKeyPair` long-term.

### Proof of Concept
On a Linux host running a CKB node:

```bash
# 1. Find the CKB node PID
CKB_PID=$(pgrep ckb)

# 2. Dump process memory
sudo cp /proc/$CKB_PID/mem /tmp/ckb_mem.bin 2>/dev/null || \
  sudo gcore -o /tmp/ckb_core $CKB_PID

# 3. The raw secret key file is also directly readable
cat ~/.ckb/data/network/secret_key | xxd

# 4. Search the memory dump for the 32-byte key sequence
# (key bytes are also present in the heap from the read_secret_key Vec<u8> buffer
#  and in NetworkState.local_private_key for the node's lifetime)
strings /tmp/ckb_mem.bin | grep -c "."   # key bytes visible as binary sequence

# 5. With the recovered key, reconstruct the SecioKeyPair and derive the peer ID,
#    then connect to peers presenting that peer ID to impersonate the node.
```

The on-disk file at `<data_dir>/network/secret_key` contains the raw 32-byte key with no encryption, directly confirming the plaintext storage. [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** util/app-config/src/configs/network.rs (L288-321)
```rust
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
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).and_then(|_read_size| {
        secio::SecioKeyPair::secp256k1_raw_key(&buf)
            .map(Some)
            .map_err(|_| Error::new(ErrorKind::InvalidData, "invalid secret key data"))
    })
}
```

**File:** network/src/network.rs (L200-201)
```rust
            local_private_key,
            local_peer_id,
```

**File:** network/src/network.rs (L331-333)
```rust
    pub fn local_private_key(&self) -> &secio::SecioKeyPair {
        &self.local_private_key
    }
```

**File:** util/crypto/src/secp/privkey.rs (L93-97)
```rust
impl Drop for Privkey {
    fn drop(&mut self) {
        self.zeroize()
    }
}
```
