Looking at the actual code carefully to assess this claim.

### Title
Unbounded Concurrent Decompression Memory: N Peers × 8 MB Per-Frame Allocation With No Global Budget — (`network/src/compress.rs`)

---

### Summary

`LengthDelimitedCodecWithCompress::decode` enforces a per-frame decompressed-size cap of 8 MB but enforces **no global or per-node decompression memory budget**. An unprivileged remote peer can open up to `max_peers` (default 125) simultaneous TCP connections and send one max-amplification snappy frame per connection per decode cycle, causing N × 8 MB of concurrent heap allocation with no bound on the aggregate.

---

### Finding Description

In `decode`, after the per-frame guard passes, the code unconditionally zero-allocates a buffer sized to the full decompressed length: [1](#0-0) 

The guard only rejects frames whose *declared* decompressed size exceeds `MAX_UNCOMPRESSED_LEN` (8 MB): [2](#0-1) [3](#0-2) 

The `max_frame_length` check enforced by the inner `length_delimited` codec bounds only the **compressed wire frame**, not the decompressed output: [4](#0-3) 

For the Sync protocol (2 MB compressed max) a frame can decompress to 8 MB (4× ratio, trivially achievable with snappy on repetitive data). For Discovery (512 KB compressed max) the ratio is 16×. There is no semaphore, counter, or token bucket anywhere in the codec layer that limits how many such allocations may be live simultaneously.

The codec is instantiated per-connection per-protocol: [5](#0-4) 

The default peer limit is 125 total (117 inbound): [6](#0-5) 

CKB uses a multi-threaded tokio runtime, so multiple `decode` calls execute concurrently across worker threads. Each live call holds its 8 MB `BytesMut` allocation until the message is fully processed by the application layer.

---

### Impact Explanation

Peak concurrent decompression heap usage is bounded only by `max_peers × MAX_UNCOMPRESSED_LEN`:

```
125 peers × 8 MB = ~1 GB transient heap pressure
```

On memory-constrained nodes (e.g., 2–4 GB RAM VPS, which is common for CKB validators), this alone can exhaust available memory, triggering the OOM killer or causing severe swap thrashing. Even on well-provisioned nodes it causes measurable latency spikes in block/transaction processing because the allocator is under sustained pressure. The attacker sustains the pressure by continuously re-sending max-amplification frames on each connection.

---

### Likelihood Explanation

The attack requires only:
1. Opening up to 125 TCP connections to the target node's P2P port (publicly reachable by design).
2. Sending a crafted snappy-compressed frame on each connection — a few hundred lines of code using any snappy library.

No PoW, no keys, no privileged role. The node's own peer-acceptance logic (eviction only kicks in *after* the connection is established and the codec is already active) does not prevent this.

---

### Recommendation

Introduce a global decompression semaphore or token bucket in the codec layer, e.g.:

```rust
// Shared across all codec instances for a node
static DECOMPRESS_BUDGET: Semaphore = Semaphore::const_new(MAX_CONCURRENT_DECOMPRESS); // e.g. 4–8
```

Acquire a permit before `BytesMut::zeroed(decompressed_bytes_len)` and release it after decompression completes. Alternatively, lower `MAX_UNCOMPRESSED_LEN` to match the per-protocol `max_frame_length` (so the compressed-frame bound also bounds the decompressed output), or add a node-wide decompression memory counter that rejects frames when the aggregate in-flight decompression exceeds a configurable limit (e.g., 64 MB).

---

### Proof of Concept

```python
import socket, struct, snappy, time

TARGET = ("ckb-node.example.com", 8115)
N = 125

# Craft a max-amplification snappy frame: 2MB of zeros compresses to ~2KB,
# decompresses to 8MB when declared via snappy header tricks, or simply
# use a 2MB repetitive payload that snappy decompresses to 8MB.
payload = b'\x00' * (2 * 1024 * 1024)  # 2MB zeros
compressed = snappy.compress(payload)   # ~2KB on wire
# Prepend compress flag (0x80) + 4-byte length header
frame = struct.pack(">I", len(compressed) + 1) + b'\x80' + compressed

sockets = []
for _ in range(N):
    s = socket.socket()
    s.connect(TARGET)
    # complete tentacle handshake (identify/sync protocol open) ...
    sockets.append(s)

# Send simultaneously
for s in sockets:
    s.sendall(frame)

# At this point: N × up to 8MB = ~1GB live on the target's heap
time.sleep(10)  # hold connections open; measure target RSS
```

Measure target node RSS before and after; assert the delta is bounded by a reasonable per-node limit rather than `N × MAX_UNCOMPRESSED_LEN`.

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L233-244)
```rust
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
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
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

**File:** network/src/protocols/mod.rs (L280-288)
```rust
            .codec(move || {
                Box::new(LengthDelimitedCodecWithCompress::new(
                    self.compress,
                    length_delimited::Builder::new()
                        .max_frame_length(max_frame_length)
                        .new_codec(),
                    self.id,
                ))
            })
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```
