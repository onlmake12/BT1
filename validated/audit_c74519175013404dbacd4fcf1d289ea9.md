Audit Report

## Title
Unchecked u64 Addition in `set_ban` Relative Ban-Time Computation Causes RPC Handler Panic — (`rpc/src/module/net.rs`)

## Summary
The `set_ban` RPC handler performs an unchecked `u64` addition of `unix_time_as_millis()` and the caller-supplied `ban_time` value. Supplying `ban_time = 0xffffffffffffffff` with `absolute = false` overflows `u64`, triggering a panic in the RPC handler task. Critically, the release profile explicitly sets `overflow-checks = true`, so the panic occurs in **both** debug and release builds — the claim's "silent wrap-around in release builds" is incorrect.

## Finding Description
In `NetRpcImpl::set_ban`, the relative-time branch at lines 710–713 computes:

```rust
unix_time_as_millis()
    + ban_time
        .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
        .value()
``` [1](#0-0) 

Both operands are `u64` with no `checked_add`, `saturating_add`, or bounds validation. `unix_time_as_millis()` returns the real system clock as `u64` milliseconds. [2](#0-1)  `ban_time` is `Option<Timestamp>` where `Timestamp = Uint64 = JsonUint<u64>`, and `.value()` returns the raw inner `u64` with no bounds check. [3](#0-2) [4](#0-3) 

The workspace `Cargo.toml` sets `overflow-checks = true` in the release profile: [5](#0-4) 

This means overflow panics in **both** debug (default) and release builds. The submitted claim that release builds produce a silent wrap-around ban-bypass is factually wrong. The actual behavior in all build configurations is a panic that unwinds the RPC handler task for that request.

No `panic = "abort"` is set in any profile, so the default `unwind` behavior applies. The panic is contained to the tokio task handling that specific request; the RPC server and node process continue running.

## Impact Explanation
The impact is a per-request panic in the `set_ban` RPC handler, causing that specific request to fail (connection reset or 500 error). The RPC service and node process remain operational. This matches the **Note (0–500 points)** category: "Any local RPC API crash." The higher-severity "ban bypass" impact claimed for release builds does not materialize because `overflow-checks = true` is set.

## Likelihood Explanation
The `set_ban` RPC requires no authentication and is bound to localhost port 8114 by default. Any local process can trigger the panic with a single JSON-RPC call. The precondition is trivially met.

## Recommendation
Replace the plain `+` with `checked_add` and return an `RPCError::invalid_params` on overflow:

```rust
let ban_until = unix_time_as_millis()
    .checked_add(
        ban_time.unwrap_or_else(|| DEFAULT_BAN_DURATION.into()).value()
    )
    .ok_or_else(|| RPCError::invalid_params("ban_time overflows u64"))?;
```

## Proof of Concept
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1, "jsonrpc": "2.0", "method": "set_ban",
    "params": ["192.168.0.1", "insert", "0xffffffffffffffff", false, null]
  }'
```

In both debug and release builds this panics the RPC handler task for that request (due to `overflow-checks = true` in the release profile). The node process itself continues running. The panic is observable as a connection reset or internal server error response.

### Citations

**File:** rpc/src/module/net.rs (L710-713)
```rust
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
```

**File:** util/systemtime/src/lib.rs (L21-26)
```rust
fn system_time_as_millis() -> u64 {
    let duration = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .expect("SystemTime before UNIX EPOCH!");
    duration.as_secs() * 1000 + u64::from(duration.subsec_millis())
}
```

**File:** util/jsonrpc-types/src/uints.rs (L22-25)
```rust
impl<T: Uint> JsonUint<T> {
    pub fn value(self) -> T {
        self.0
    }
```

**File:** util/jsonrpc-types/src/primitive.rs (L38-43)
```rust
/// The Unix timestamp in milliseconds (1 second is 1000 milliseconds).
///
/// For example, 1588233578000 is Thu, 30 Apr 2020 07:59:38 +0000
///
/// This is a 64-bit unsigned integer type encoded as the 0x-prefixed hex string in JSON. See examples of [Uint64](type.Uint64.html#examples).
pub type Timestamp = Uint64;
```

**File:** Cargo.toml (L318-319)
```text
[profile.release]
overflow-checks = true
```
