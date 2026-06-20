### Title
Node P2P Network Identity Secret Key Stored as Unencrypted Plaintext on Disk — (`util/app-config/src/configs/network.rs`)

---

### Summary
CKB's node network identity secret key (a 32-byte secp256k1 raw key) is written to disk as unencrypted plaintext bytes via `write_secret_to_file`. The only protection is filesystem permissions. An attacker who gains read access to the node's data directory — via unencrypted backups, a co-located service running as the same OS user, or misconfigured permissions — can steal the key and fully impersonate the node in the P2P network.

---

### Finding Description

When a CKB node starts for the first time, `Config::fetch_private_key` is called in `NetworkState::from_config`. If no key file exists, `write_secret_key_to_file` is invoked, which calls `write_secret_to_file` with the raw 32-byte key material:

```rust
// util/app-config/src/configs/network.rs
pub fn write_secret_to_file(secret: &[u8], path: PathBuf) -> Result<(), Error> {
    fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
        .and_then(|mut file| {
            file.write_all(secret)?;   // raw bytes, no encryption
            ...
            file.set_permissions(fs::Permissions::from_mode(0o400))
        })
}
``` [1](#0-0) 

The key is stored at `<data_dir>/network/secret_key` as raw unencrypted bytes. On subsequent starts, `read_secret_key` reads those bytes back directly:

```rust
file.read_to_end(&mut buf).and_then(|_read_size| {
    secio::SecioKeyPair::secp256k1_raw_key(&buf)
        .map(Some)
        ...
})
``` [2](#0-1) 

The permission check in `read_secret_key` only emits a **warning** if permissions are wrong — it does not abort or refuse to load the key. On non-Unix systems, the only protection is the `readonly` attribute, which prevents writes but does not restrict reads by other processes with appropriate OS-level access. [3](#0-2) 

This key is loaded into `NetworkState::local_private_key` and used for all secio P2P authentication:

```rust
// network/src/network.rs
let local_private_key = config.fetch_private_key()?;
...
Ok(NetworkState {
    ...
    local_private_key,
    local_peer_id,
    ...
})
``` [4](#0-3) 

A second instance of the same pattern exists for the Tor onion service private key in `create_tor_secret_key` / `load_tor_secret_key`: the 64-byte `TorSecretKeyV3` is stored as base64-encoded plaintext (no encryption, only 0o600 permissions on Unix) at `<data_dir>/network/onion_private_key`. [5](#0-4) 

---

### Impact Explanation

The secp256k1 secret key is the node's permanent P2P identity. Its public key hash is the node's `PeerId`. An attacker who obtains the raw key file can:

1. **Impersonate the node** — reconstruct the identical `SecioKeyPair` and present the same `PeerId` to any peer, including bootnodes and long-term peers that have this node in their peer store.
2. **Eclipse attack** — if the compromised node is a well-known bootnode or relay node, the attacker can stand up a rogue node with the same identity, intercept discovery traffic, and feed manipulated block/transaction data to nodes that connect to it.
3. **Deanonymize onion-service nodes** — stealing the `onion_private_key` reveals the node's `.onion` address and allows the attacker to impersonate the hidden service, breaking the anonymity guarantee of the Tor integration. [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The file is protected by OS permissions (0o400 on Unix), so direct theft requires either running as the same OS user as the CKB process or having root access. However, realistic attack vectors that do not require interactive privileged access include:

- **Unencrypted backups**: Any backup agent running as root or as the same user copies the raw key file. Cloud snapshot backups of the data volume are a common example.
- **Co-located service compromise**: A web server, RPC proxy, or monitoring agent running as the same OS user can read the file.
- **Non-Unix deployments**: On Windows, `readonly` only prevents modification; other processes with normal read access can read the file freely.
- **Misconfigured permissions**: The permission check in `read_secret_key` only warns, so a misconfigured deployment silently continues with a world-readable key file. [7](#0-6) 

---

### Recommendation

**Short term:**
- Encrypt the secret key with a symmetric key derived from a user-supplied password (e.g., via stdin at startup or an environment variable) before writing it to disk. Decrypt in memory at startup only.
- On non-Unix platforms, enforce ACL-based access control rather than relying solely on the `readonly` attribute.
- Treat a wrong-permission key file as a fatal error rather than a warning.

**Long term:**
- Integrate with OS-level secret stores (e.g., Linux kernel keyring, macOS Keychain, Windows DPAPI) or an external secrets manager (e.g., HashiCorp Vault) to avoid storing key material on the filesystem at all.
- Apply the same hardening to the onion service private key in `util/onion/src/onion_service.rs`.

---

### Proof of Concept

1. CKB node starts; `write_secret_to_file` writes 32 raw bytes to `<data_dir>/network/secret_key`. [8](#0-7) 

2. Attacker reads the file (e.g., from an unencrypted backup or as the same OS user):
   ```bash
   cat /path/to/ckb/data/network/secret_key | xxd
   ```

3. Attacker reconstructs the identical key pair:
   ```rust
   let stolen_bytes = std::fs::read("/path/to/ckb/data/network/secret_key").unwrap();
   let key_pair = secio::SecioKeyPair::secp256k1_raw_key(&stolen_bytes).unwrap();
   // key_pair.peer_id() == victim node's PeerId
   ```

4. Attacker launches a rogue CKB node using the stolen `SecioKeyPair` as `local_private_key`, presenting the victim's `PeerId` to the network. Peers that have the victim in their peer store will accept connections from the rogue node, enabling eclipse or data-manipulation attacks. [9](#0-8)

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

**File:** util/app-config/src/configs/network.rs (L293-314)
```rust
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

**File:** util/app-config/src/configs/network.rs (L315-321)
```rust
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).and_then(|_read_size| {
        secio::SecioKeyPair::secp256k1_raw_key(&buf)
            .map(Some)
            .map_err(|_| Error::new(ErrorKind::InvalidData, "invalid secret key data"))
    })
}
```

**File:** network/src/network.rs (L83-84)
```rust
    local_private_key: secio::SecioKeyPair,
    local_peer_id: PeerId,
```

**File:** network/src/network.rs (L97-157)
```rust
    pub fn from_config(config: NetworkConfig) -> Result<NetworkState, Error> {
        config.create_dir_if_not_exists()?;
        let local_private_key = config.fetch_private_key()?;
        let local_peer_id = local_private_key.peer_id();
        // set max score to public addresses
        let public_addrs: HashSet<Multiaddr> = config
            .listen_addresses
            .iter()
            .chain(config.public_addresses.iter())
            .cloned()
            .filter_map(|mut addr| match multiaddr_to_socketaddr(&addr) {
                Some(socket_addr) if !is_reachable(socket_addr.ip()) => None,
                _ => {
                    match extract_peer_id(&addr) {
                        Some(peer_id) if peer_id != local_peer_id => {
                            error!("Don't include addresses that not associated with this node in the public_addresses list: {:?}", addr);
                            std::process::exit(1);
                        }
                        Some(_) => (),
                        None => addr.push(Protocol::P2P(Cow::Borrowed(local_peer_id.as_bytes()))),
                    }
                    Some(addr)
                }
            })
            .collect();
        info!("Loading the peer store. This process may take a few seconds to complete.");

        let peer_store = Mutex::new(PeerStore::load_from_dir_or_default(
            config.peer_store_path(),
        ));
        info!("Loaded the peer store.");

        if let Some(ref proxy_url) = config.proxy.proxy_url {
            proxy::check_proxy_url(proxy_url).map_err(Error::Config)?;
        }

        let bootnodes = config.bootnodes();

        let peer_registry = PeerRegistry::new(
            config.max_inbound_peers(),
            config.max_outbound_peers(),
            config.whitelist_only,
            config.whitelist_peers(),
            config.disable_block_relay_only_connection,
        );

        Ok(NetworkState {
            peer_store,
            config,
            bootnodes,
            peer_registry: RwLock::new(peer_registry),
            dialing_addrs: RwLock::new(HashMap::default()),
            public_addrs: RwLock::new(public_addrs),
            listened_addrs: RwLock::new(Vec::new()),
            observed_addrs: RwLock::new(HashMap::default()),
            local_private_key,
            local_peer_id,
            active: AtomicBool::new(true),
            protocols: RwLock::new(Vec::new()),
            required_flags: Flags::SYNC | Flags::DISCOVERY | Flags::RELAY,
        })
```

**File:** util/onion/src/onion_service.rs (L163-196)
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
```
