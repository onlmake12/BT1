### Title
P2P Secret Key Written World-Readable Before Permission Restriction (TOCTOU) - (File: `util/app-config/src/configs/network.rs`)

### Summary
`write_secret_to_file` creates the node's secp256k1 P2P secret key file with default (umask-derived, typically world-readable) permissions, writes the raw 32-byte key, and only then restricts permissions to `0o400`. Any local user on the same host can race to read the file during this window and steal the node's P2P identity key.

### Finding Description
`write_secret_to_file` in `util/app-config/src/configs/network.rs` uses a two-step approach:

1. Open/create the file with `create(true).write(true).truncate(true)` — no mode is specified, so the OS applies the process umask (typically `0o644` on Linux with umask `0o022`).
2. Write the raw 32-byte secret key bytes.
3. **Only then** call `file.set_permissions(fs::Permissions::from_mode(0o400))`. [1](#0-0) 

Between steps 1 and 3 there is a measurable window during which the file exists on disk with world-readable permissions and already contains the plaintext secret key. A local attacker monitoring the directory with `inotify` (Linux) or `kqueue` (macOS) can open and read the file the instant it is created, before `set_permissions` is called.

By contrast, `create_tor_secret_key` in `util/onion/src/onion_service.rs` correctly uses `OpenOptionsExt::mode(0o600)` atomically at file-open time on Unix, eliminating the window entirely. [2](#0-1) 

The vulnerable function is called in two production paths:
- `Config::write_secret_key_to_file` → `Config::fetch_private_key` (called at node startup via `NetworkState::from_config`)
- `Setup::generate` (called via `ckb peer-id gen` CLI) [3](#0-2) [4](#0-3) 

### Impact Explanation
The stolen key is the node's secp256k1 P2P identity key. With it an attacker can:
- Derive the same `PeerId` and impersonate the node on the CKB P2P network.
- Decrypt any previously recorded secio-encrypted P2P sessions that used this key pair.
- Establish connections that peers believe originate from the legitimate node, enabling targeted eclipse or man-in-the-middle attacks against that node's peers. [5](#0-4) 

### Likelihood Explanation
The attack is straightforward on any multi-user Linux/macOS host (shared cloud VM, container host, CI runner). The attacker needs only:
1. A shell account on the same machine.
2. An `inotify_add_watch` call on the network config directory.
3. An `open`/`read` call on the newly created file — all achievable in a few microseconds, well within the TOCTOU window.

The window exists every time the node is initialized for the first time (no existing `secret_key` file), which is a one-time but deterministic event.

### Recommendation
Replace the two-step create-then-chmod with an atomic single-step open that sets the mode at creation, mirroring the pattern already used in `create_tor_secret_key`:

```rust
// Unix
use std::os::unix::fs::OpenOptionsExt;
fs::OpenOptions::new()
    .create_new(true)   // fail if file already exists
    .write(true)
    .mode(0o400)        // set restrictive mode atomically at creation
    .open(path)
    .and_then(|mut file| file.write_all(secret))
```

Using `create_new(true)` additionally prevents truncation of an existing key file by a concurrent process. On non-Unix platforms, the closest equivalent is to write to a temporary file and atomically rename it after setting permissions.

### Proof of Concept
```bash
# Terminal 1 – attacker (local unprivileged user)
inotifywait -m -e create /path/to/ckb/network/ &
# When the CREATE event fires for "secret_key":
cat /path/to/ckb/network/secret_key | xxd   # raw 32-byte key readable before chmod

# Terminal 2 – victim
ckb init -C /path/to/ckb   # triggers write_secret_to_file on first run
```

The `inotify` CREATE event fires after `open()` returns but before `set_permissions` is called. The attacker's `cat` executes during this window and reads the plaintext key bytes. [6](#0-5)

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

**File:** util/app-config/src/configs/network.rs (L384-389)
```rust
    #[cfg(not(target_family = "wasm"))]
    fn write_secret_key_to_file(&self) -> Result<(), Error> {
        let path = self.secret_key_path();
        let random_key_pair = generate_random_key();
        write_secret_to_file(&random_key_pair, path)
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

**File:** network/src/network.rs (L99-100)
```rust
        let local_private_key = config.fetch_private_key()?;
        let local_peer_id = local_private_key.peer_id();
```
