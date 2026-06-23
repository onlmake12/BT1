### Title
Unconstrained `ban_time` Parameter in `set_ban` RPC Causes Silent Integer Overflow, Rendering Peer Bans Ineffective — (File: `rpc/src/module/net.rs`)

---

### Summary

The `set_ban` RPC method in `rpc/src/module/net.rs` accepts an unconstrained `ban_time` (`Uint64`, milliseconds) parameter with no upper-bound validation. When `absolute` is `false` (the default), the implementation performs a plain `u64 + u64` addition of the current timestamp and the caller-supplied `ban_time`. Passing `u64::MAX` (or any value large enough to overflow) causes the result to silently wrap in release builds, producing a `ban_until` timestamp that is in the past. The peer is never actually banned.

---

### Finding Description

In `rpc/src/module/net.rs`, the `set_ban` implementation computes `ban_until` as:

```rust
let ban_until = if absolute.unwrap_or(false) {
    ban_time.unwrap_or_default().into()
} else {
    unix_time_as_millis()
        + ban_time
            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
            .value()
};
``` [1](#0-0) 

When `absolute` is `false`, the code adds `unix_time_as_millis()` (a `u64` representing the current epoch in milliseconds, currently ≈ 1,750,000,000,000) to the caller-supplied `ban_time.value()` (also a `u64`). There is no overflow check, no maximum bound, and no validation of the `ban_time` argument beyond parsing the IP address and command string. [2](#0-1) 

In Rust release builds, `u64` arithmetic wraps on overflow. If a caller passes `ban_time = u64::MAX`:

```
ban_until = 1_750_000_000_000 + 18_446_744_073_709_551_615
          = 18_446_744_075_459_551_615  (overflows u64::MAX)
          ≡ 1_749_999_999_999  (mod 2^64)
```

The resulting `ban_until` ≈ 1,749,999,999,999 ms, which is approximately 1 ms **before** the current time. The ban entry is written to the peer store with an already-expired timestamp, so the peer is never actually blocked.

The `ban_network` function in `network/src/peer_store/peer_store_impl.rs` stores the `ban_until` value directly without any sanity check:

```rust
let ban_addr = BannedAddr {
    address: network,
    ban_until: now_ms + timeout_ms,
    ...
};
``` [3](#0-2) 

The `is_addr_banned` check compares the current time against `ban_until`, so an already-expired entry is treated as not banned.

---

### Impact Explanation

**Impact: Medium.** A node operator who attempts to impose a very long (or "permanent") ban on a malicious peer by supplying a large `ban_time` value will silently fail to ban the peer. The malicious peer can immediately reconnect and continue P2P-level attacks: relaying invalid blocks/headers, spamming the tx-pool, or exhausting connection slots. The operator receives no error and has no indication the ban was ineffective. In debug builds, the overflow panics, crashing the RPC handler.

---

### Likelihood Explanation

**Likelihood: Low.** This requires a local RPC user (node operator) to supply an extreme `ban_time` value, either by mistake ("fat-finger") or by intentionally trying to set a permanent ban using `u64::MAX`. The `set_ban` RPC is enabled by default in the `Net` module and is accessible to any local RPC caller. [4](#0-3) 

---

### Recommendation

Replace the plain `+` with a saturating or checked addition, and add an explicit upper-bound validation on `ban_time`:

```rust
// Reject ban_time values that would overflow or exceed a reasonable maximum
const MAX_BAN_DURATION_MS: u64 = 365 * 24 * 60 * 60 * 1000; // 1 year
if ban_time.map(|t| t.value()).unwrap_or(0) > MAX_BAN_DURATION_MS {
    return Err(RPCError::invalid_params("ban_time exceeds maximum allowed value"));
}
let ban_until = unix_time_as_millis()
    .saturating_add(ban_time.unwrap_or_else(|| DEFAULT_BAN_DURATION.into()).value());
```

---

### Proof of Concept

1. Start a CKB node with the `Net` RPC module enabled (default).
2. Call `set_ban` with a huge `ban_time` and `absolute = false`:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "set_ban",
  "params": [
    "192.168.1.100",
    "insert",
    "0xffffffffffffffff",
    false,
    "permanent ban attempt"
  ]
}
```

3. Immediately call `get_banned_addresses`. The entry will appear with `ban_until` ≈ current time − 1 ms (already expired).
4. The peer at `192.168.1.100` can reconnect immediately; `is_addr_banned` returns `false`.

The root cause is the unchecked `u64` addition at `rpc/src/module/net.rs` lines 710–713, with no maximum bound enforced on the `ban_time` parameter anywhere in the call path. [5](#0-4)

### Citations

**File:** rpc/src/module/net.rs (L335-343)
```rust
    #[rpc(name = "set_ban")]
    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()>;
```

**File:** rpc/src/module/net.rs (L699-727)
```rust
        let ip_network = address.parse().map_err(|_| {
            RPCError::invalid_params(format!(
                "Expected `params[0]` to be a valid IP address, got {address}"
            ))
        })?;

        match command.as_ref() {
            "insert" => {
                let ban_until = if absolute.unwrap_or(false) {
                    ban_time.unwrap_or_default().into()
                } else {
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
                };
                self.network_controller
                    .ban(ip_network, ban_until, reason.unwrap_or_default());
                Ok(())
            }
            "delete" => {
                self.network_controller.unban(&ip_network);
                Ok(())
            }
            _ => Err(RPCError::invalid_params(format!(
                "Expected `params[1]` to be in the list [insert, delete], got {address}"
            ))),
        }
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L294-303)
```rust
    pub(crate) fn ban_network(&mut self, network: IpNetwork, timeout_ms: u64, ban_reason: String) {
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let ban_addr = BannedAddr {
            address: network,
            ban_until: now_ms + timeout_ms,
            created_at: now_ms,
            ban_reason,
        };
        self.mut_ban_list().ban(ban_addr);
    }
```
