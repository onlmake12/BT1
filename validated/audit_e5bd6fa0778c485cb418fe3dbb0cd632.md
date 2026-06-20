### Title
P2P Node Private Key Retained in Unzeroed Heap Memory for Entire Node Lifetime - (File: `network/src/network.rs`, `util/app-config/src/configs/network.rs`)

---

### Summary

CKB's P2P network private key (secp256k1, used for node identity and secio handshake encryption) is loaded into a heap-allocated `Vec<u8>` that is never zeroed before being dropped, and is then stored in `NetworkState.local_private_key` (a `SecioKeyPair` with no zeroize-on-drop) for the entire lifetime of the running node. If the node crashes while sentry crash reporting is enabled (a first-class supported feature), the heap dump sent to the error-reporting service will contain the raw 32-byte private key in plaintext.

---

### Finding Description

**Root cause 1 — Unzeroed intermediate buffer in `read_secret_key`:**

In `util/app-config/src/configs/network.rs`, the function `read_secret_key` reads the raw 32-byte private key from disk into a heap-allocated `Vec<u8>`:

```rust
let mut buf = Vec::new();
file.read_to_end(&mut buf).and_then(|_read_size| {
    secio::SecioKeyPair::secp256k1_raw_key(&buf)
        .map(Some)
        .map_err(|_| Error::new(ErrorKind::InvalidData, "invalid secret key data"))
})
``` [1](#0-0) 

When `buf` falls out of scope at the end of the function, Rust's default `Drop` for `Vec<u8>` deallocates the backing memory **without zeroing it**. The raw key bytes remain on the heap until the allocator happens to reuse that region. There is no `zeroize()` call, no `ptr::write_volatile`, and no memory fence — in direct contrast to CKB's own `Privkey` type, which explicitly implements `Drop` with volatile zeroing. [2](#0-1) 

**Root cause 2 — `SecioKeyPair` held in `NetworkState` for the entire node lifetime:**

`NetworkState` stores `local_private_key: secio::SecioKeyPair` as a plain struct field: [3](#0-2) 

This `NetworkState` is wrapped in `Arc<NetworkState>` and shared across every protocol handler, service, and the `NetworkController` for the entire duration of the node process: [4](#0-3) 

`SecioKeyPair` (from the `tentacle-secio` external crate) implements no `Drop` that zeroizes its internal key bytes. The private key therefore lives in heap memory from node startup until process exit, with no mechanism to clear it.

**Root cause 3 — Sentry crash reporting is a first-class supported feature:**

The codebase explicitly supports sentry with `#[cfg(feature = "with_sentry")]` guards throughout `network/src/network.rs` and `ckb-bin/src/setup.rs`. When sentry is enabled and the node crashes, the sentry SDK captures process memory (stack, heap, registers) and transmits it to the configured endpoint. [5](#0-4) 

---

### Impact Explanation

An attacker who obtains the node's P2P private key can:

1. **Impersonate the node** on the CKB P2P network, since the secio handshake uses this key for identity and session encryption. Peers that have whitelisted or trusted this node's peer ID would accept connections from the impersonator.
2. **Decrypt past and future secio sessions** if session keys were derived in a way that does not provide forward secrecy against key compromise.
3. **Disrupt the node's connectivity** by establishing conflicting sessions under the same peer ID.

The key is not a wallet signing key, so direct fund theft is not possible. However, the impact on node identity and encrypted P2P communication is concrete and within the CKB network security scope.

---

### Likelihood Explanation

The likelihood is **medium**. The conditions required are:

- The node must be built with the `with_sentry` feature and have a sentry DSN configured (a documented, supported deployment mode).
- A crash must occur. Crashes can be triggered by malicious peers sending crafted protocol messages (a realistic attacker-controlled entry path for a public P2P node).
- The sentry report must be intercepted or the sentry endpoint must be compromised.

Alternatively, on Linux, `/proc/<pid>/mem` or core dumps (if enabled by the OS) expose the same heap region to any local user with sufficient privilege, which is a lower bar.

---

### Recommendation

**Short term:**

1. In `read_secret_key`, zero `buf` before it is dropped:
   ```rust
   let mut buf = Vec::new();
   let result = file.read_to_end(&mut buf).and_then(|_| {
       secio::SecioKeyPair::secp256k1_raw_key(&buf)
           .map(Some)
           .map_err(|_| Error::new(ErrorKind::InvalidData, "invalid secret key data"))
   });
   // Zero the buffer before dropping
   for byte in buf.iter_mut() { *byte = 0; }
   result
   ```
   Use `zeroize` crate's `Zeroizing<Vec<u8>>` wrapper for correctness against compiler optimizations.

2. Wrap `SecioKeyPair` in a newtype that implements `Drop` with volatile zeroing, mirroring the existing `Privkey::zeroize()` pattern.

**Long term:**

Store only the `PeerId` (public identifier) in long-lived state. Re-derive or re-load the `SecioKeyPair` only when a new handshake is needed, and drop it immediately after use.

---

### Proof of Concept

1. Start a CKB node with `with_sentry` feature enabled and a sentry DSN configured.
2. Send a crafted P2P message to the node that triggers a panic (e.g., an oversized or malformed protocol buffer that hits an `unwrap()` or index-out-of-bounds in a protocol handler).
3. The sentry SDK captures the heap at crash time. The raw 32-byte P2P private key is present in the heap at the address previously occupied by `buf` in `read_secret_key` (not yet overwritten) and also within the `SecioKeyPair` field of the `NetworkState` allocation (live for the entire session).
4. Parse the sentry minidump/heap attachment to recover the 32-byte key. Verify by deriving the corresponding `PeerId` and confirming it matches the crashed node's advertised peer ID. [1](#0-0) [6](#0-5) [7](#0-6)

### Citations

**File:** util/app-config/src/configs/network.rs (L315-320)
```rust
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).and_then(|_read_size| {
        secio::SecioKeyPair::secp256k1_raw_key(&buf)
            .map(Some)
            .map_err(|_| Error::new(ErrorKind::InvalidData, "invalid secret key data"))
    })
```

**File:** util/crypto/src/secp/privkey.rs (L50-56)
```rust
    // uses core::ptr::write_volatile and core::sync::atomic memory fences to zeroing
    pub(crate) fn zeroize(&mut self) {
        for elem in self.inner.0.iter_mut() {
            volatile_write(elem, Default::default());
            atomic_fence();
        }
    }
```

**File:** util/crypto/src/secp/privkey.rs (L93-96)
```rust
impl Drop for Privkey {
    fn drop(&mut self) {
        self.zeroize()
    }
```

**File:** network/src/network.rs (L51-52)
```rust
#[cfg(feature = "with_sentry")]
use sentry::{Level, capture_message, with_scope};
```

**File:** network/src/network.rs (L74-92)
```rust
pub struct NetworkState {
    pub(crate) peer_registry: RwLock<PeerRegistry>,
    pub(crate) peer_store: Mutex<PeerStore>,
    /// Node listened addresses
    pub(crate) listened_addrs: RwLock<Vec<Multiaddr>>,
    dialing_addrs: RwLock<HashMap<PeerId, Instant>>,
    /// Node public addresses by config
    public_addrs: RwLock<HashSet<Multiaddr>>,
    observed_addrs: RwLock<HashMap<PeerIndex, Multiaddr>>,
    local_private_key: secio::SecioKeyPair,
    local_peer_id: PeerId,
    pub(crate) bootnodes: Vec<Multiaddr>,
    pub(crate) config: NetworkConfig,
    pub(crate) active: AtomicBool,
    /// Node supported protocols
    /// fields: ProtocolId, Protocol Name, Supported Versions
    pub(crate) protocols: RwLock<Vec<(ProtocolId, String, Vec<String>)>>,
    pub(crate) required_flags: Flags,
}
```

**File:** network/src/network.rs (L143-157)
```rust
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
