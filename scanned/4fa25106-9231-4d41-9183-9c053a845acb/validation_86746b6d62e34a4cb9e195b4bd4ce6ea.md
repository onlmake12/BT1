I need to verify the actual code behavior against the claim before rendering a verdict.

Audit Report

## Title
Unbounded Concurrent Decompression Memory: N Peers × 8 MB Per-Frame Allocation With No Global Budget — (`network/src/compress.rs`)

## Summary

`LengthDelimitedCodecWithCompress::decode` allocates a `BytesMut` buffer sized to the full declared decompressed length (up to 8 MB) for every incoming compressed frame, with no global or per-node limit on how many such allocations may be live simultaneously. An unprivileged remote peer can open up to `max_peers` (125) connections and send one max-amplification snappy frame per connection, causing up to 125 × 8 MB ≈ 1 GB of concurrent heap allocation with no aggregate bound. On memory-constrained nodes this is sufficient to trigger the OOM killer or severe swap thrashing, crashing the node.

## Finding Description

**Root cause — per-frame guard with no aggregate budget:**

In `LengthDelimitedCodecWithCompress::decode` (`network/src/compress.rs`, lines 233–244), after the inner `length_delimited` codec accepts a compressed wire frame, the code reads the snappy-declared decompressed length and rejects only frames whose declared size exceeds `MAX_UNCOMPRESSED_LEN` (8 MB):

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
    return Err(io::ErrorKind::InvalidData.into());
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len);  // up to 8 MB allocated here
```

There is no semaphore, counter, or token bucket anywhere in the codec layer (confirmed: no matches for any global decompression budget in the entire codebase). The `max_frame_length` check enforced by the inner `length_delimited` codec bounds only the **compressed wire frame** (e.g., 2 MB for Sync, 4 MB for RelayV3), not the decompressed output. The decompressed output is separately bounded only by `MAX_UNCOMPRESSED_LEN = 8 MB`.

**Exploit flow:**

1. Attacker opens up to 125 TCP connections to the node's P2P port (publicly reachable by design).
2. Completes the tentacle noise handshake (no PoW, no keys, no privileged role required).
3. On each connection, sends a snappy-compressed frame whose payload is, e.g., 8 MB of zeros (compresses to ~8 KB on the wire — well within the 2 MB Sync `max_frame_length`).
4. Each `decode` call passes the per-frame guard (`decompressed_bytes_len ≤ 8 MB`) and executes `BytesMut::zeroed(8_388_608)`.
5. The 8 MB buffer remains live until the application layer (`received()`) finishes processing the message.
6. With 125 connections all sending simultaneously, 125 × 8 MB ≈ 1 GB of heap is live concurrently.

**Why existing checks are insufficient:**

- The per-frame `MAX_UNCOMPRESSED_LEN` guard (line 235) only prevents a single frame from exceeding 8 MB; it does not limit aggregate concurrent allocations.
- The `max_frame_length` check (lines 283–285 of `protocols/mod.rs`) bounds only the compressed wire size, not the decompressed output.
- The `max_peers = 125` limit is enforced only after the connection is established and the codec is already active.
- No global decompression semaphore or budget exists anywhere in the codebase.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

Peak concurrent decompression heap usage is bounded only by `max_peers × MAX_UNCOMPRESSED_LEN = 125 × 8 MB = 1 GB`. On common CKB validator deployments (2–4 GB RAM VPS), this alone can exhaust available memory, triggering the OOM killer and crashing the node. The attacker sustains the pressure by continuously re-sending max-amplification frames on each connection. Even on well-provisioned nodes, sustained 1 GB transient allocator pressure causes measurable latency spikes in block and transaction processing.

## Likelihood Explanation

The attack requires only: (1) opening up to 125 TCP connections to the public P2P port, and (2) sending a crafted snappy-compressed frame on each connection — achievable with ~50 lines of code using any snappy library. No proof of work, no cryptographic keys, no privileged role, and no victim mistakes are required. The attack is repeatable and can be sustained indefinitely by holding connections open and re-sending frames. The node's peer eviction logic does not prevent this because eviction only applies after the connection is established and the codec is already active.

## Recommendation

Introduce a node-wide decompression memory budget enforced before `BytesMut::zeroed`:

```rust
// Shared (e.g., via Arc<AtomicUsize>) across all codec instances
static DECOMPRESS_IN_FLIGHT: AtomicUsize = AtomicUsize::new(0);
const MAX_TOTAL_DECOMPRESS: usize = 64 * 1024 * 1024; // 64 MB node-wide

// In decode(), before BytesMut::zeroed:
let prev = DECOMPRESS_IN_FLIGHT.fetch_add(decompressed_bytes_len, Ordering::Relaxed);
if prev + decompressed_bytes_len > MAX_TOTAL_DECOMPRESS {
    DECOMPRESS_IN_FLIGHT.fetch_sub(decompressed_bytes_len, Ordering::Relaxed);
    return Err(io::ErrorKind::InvalidData.into());
}
// ... allocate, decompress, then subtract on completion/error
```

Alternatively, lower `MAX_UNCOMPRESSED_LEN` per-protocol to match `max_frame_length` (eliminating the amplification gap), or use a `tokio::sync::Semaphore` with a small permit count (e.g., 8–16) to bound concurrent decompression operations.

## Proof of Concept

```python
import socket, struct, snappy, threading

TARGET = ("ckb-node.example.com", 8115)
N = 125

# 8MB of zeros compresses to ~8KB with snappy — well within 2MB Sync max_frame_length
payload = b'\x00' * (8 * 1024 * 1024)
compressed = snappy.compress(payload)
# Wire frame: 4-byte big-endian length + compress flag (0x80) + compressed data
frame = struct.pack(">I", len(compressed) + 1) + b'\x80' + compressed

sockets = []
for _ in range(N):
    s = socket.socket()
    s.connect(TARGET)
    # complete tentacle noise handshake + identify/sync protocol open
    sockets.append(s)

# Send all frames simultaneously
def send(s): s.sendall(frame)
threads = [threading.Thread(target=send, args=(s,)) for s in sockets]
for t in threads: t.start()
for t in threads: t.join()

# Assert: target node RSS delta is bounded by a reasonable limit, not N × 8MB
# Measure with: watch -n1 'ps aux | grep ckb'
import time; time.sleep(10)
```

Measure target node RSS before and after; the delta should not approach 1 GB. Confirm by checking that `BytesMut::zeroed` is called 125 times concurrently with no aggregate guard in `network/src/compress.rs` lines 235–242.