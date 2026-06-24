Audit Report

## Title
Unconditional 1-Byte Frame Triggers Immediate 300-Second IP Ban via `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

## Summary
The `data.len() < 2` guard in `LengthDelimitedCodecWithCompress::decode` fires unconditionally regardless of `enable_compress`. A remote peer that completes the secio handshake and sends a 5-byte TCP payload (`[0x00,0x00,0x00,0x01,0x00]`) causes the codec to return `Err(io::ErrorKind::InvalidData)`, which the tentacle framework surfaces as `ServiceError::ProtocolError`, which `handle_error` in `network.rs` converts into an unconditional 300-second IP ban with no prior misbehavior scoring required. An attacker with N distinct IPs can ban N IPs from the victim's peer store simultaneously, continuously degrading peer diversity and inbound connection capacity.

## Finding Description

**Step 1 — Unconditional codec guard**

The check at `compress.rs:228` is not gated on `self.enable_compress`. Any frame whose length-delimited payload is exactly 1 byte (flag byte only, no application payload) satisfies `max_frame_length` for every protocol but fails `data.len() < 2`, returning `Err(io::ErrorKind::InvalidData)`: [1](#0-0) 

**Step 2 — Codec is always installed for all CKB protocols**

`CKBProtocol::build()` always wraps the inner `LengthDelimitedCodec` with `LengthDelimitedCodecWithCompress`, regardless of the `compress` flag: [2](#0-1) 

All protocols have `max_frame_length` values far above 1 byte (1 KB to 4 MB), so the malformed frame passes the length check and reaches the `data.len() < 2` guard: [3](#0-2) 

**Step 3 — Codec error → unconditional 300-second ban**

`ServiceError::ProtocolError` is handled with an unconditional `ban_session` call at a hardcoded 300-second duration, with no misbehavior scoring or threshold: [4](#0-3) 

**Step 4 — `ban_session` bans the peer's exact IP**

`ban_session` looks up the session's connected address and calls `peer_store.lock().ban_addr(...)` for non-whitelisted peers: [5](#0-4) 

The ban is stored as an exact `/32` (IPv4) or `/128` (IPv6) `IpNetwork` entry via `ip_to_network`: [6](#0-5) 

The `BanList` checks both exact-IP and network-containment on every inbound connection attempt: [7](#0-6) 

**Existing guards are insufficient**: The `is_whitelist` filter in `ban_session` only protects explicitly whitelisted peers. Ordinary inbound peers have no protection. No rate-limiting or misbehavior scoring precedes the ban decision for codec-level errors.

## Impact Explanation

This is a **High** severity issue matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker controlling N distinct IP addresses can ban all N IPs from the victim's peer store simultaneously, each for 300 seconds. By cycling across IP pools, the attacker continuously degrades the victim node's peer diversity and inbound connection capacity. At sufficient scale (e.g., rotating through large IP pools), this can effectively isolate a target node from legitimate peers, constituting a sustained low-cost DoS against individual CKB nodes and degrading overall network connectivity.

## Likelihood Explanation

- Requires only a TCP connection and a valid secio handshake — no privileged access, no PoW, no special keys.
- The attack payload is exactly 5 bytes: `[0x00, 0x00, 0x00, 0x01, 0x00]`.
- The existing unit test at `network/src/tests/compress.rs` explicitly confirms this byte sequence triggers the error.
- No rate-limiting or scoring precedes the ban; each connection attempt results in an immediate ban.
- Cloud providers and residential proxy networks make N-IP attacks cheap and accessible to unprivileged attackers.

## Recommendation

1. **Do not ban on codec errors without prior scoring.** Codec-level `InvalidData` should increment a misbehavior score; only ban after the score exceeds a threshold.
2. **Alternatively**, treat a codec error as a disconnect (not a ban), consistent with how `ServiceError::MuxerError` and `ServiceError::SessionTimeout` are handled (neither triggers a ban).
3. **Or**, add a minimum-frame-size check inside the tentacle codec layer before the application-level `data.len() < 2` guard, so that a 1-byte frame is silently dropped rather than treated as peer misbehavior.

## Proof of Concept

```
# 1. Connect to victim node and complete secio handshake (standard p2p negotiation)
# 2. Open any CKB application protocol substream (e.g., /ckb/syn/1 or /ckb/relay3/1)
# 3. Send exactly 5 bytes over the substream:
#      [0x00, 0x00, 0x00, 0x01]  <- 4-byte big-endian length prefix = 1
#      [0x00]                    <- 1-byte payload (flag byte only, no application data)
#
# Result: victim's decode() hits data.len() < 2 at compress.rs:228, returns Err(InvalidData)
# Tentacle emits ServiceError::ProtocolError
# handle_error calls ban_session(id, Duration::from_secs(300), ...)
# peer_store records ban for attacker's /32 IP for 300 seconds
#
# Verify: call get_banned_addresses RPC on victim node
#         attacker IP appears with ban_until = now + 300s
#
# Repeat from 100 distinct IPs → 100 IPs banned simultaneously
# Cycle every 300s across a large IP pool for sustained peer degradation
```

### Citations

**File:** network/src/compress.rs (L226-230)
```rust
        match self.length_delimited.decode(src)? {
            Some(mut data) => {
                if data.len() < 2 {
                    return Err(io::ErrorKind::InvalidData.into());
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

**File:** network/src/protocols/support_protocols.rs (L122-136)
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
```

**File:** network/src/network.rs (L248-268)
```rust
        if let Some(addr) = self.with_peer_registry(|reg| {
            reg.get_peer(session_id)
                .filter(|peer| !peer.is_whitelist)
                .map(|peer| peer.connected_addr.clone())
        }) {
            info!(
                "Ban peer {:?} for {} seconds, reason: {}",
                addr,
                duration.as_secs(),
                reason
            );
            if let Some(metrics) = ckb_metrics::handle() {
                metrics.ckb_network_ban_peer.inc();
            }
            if let Some(peer) = self.with_peer_registry_mut(|reg| reg.remove_peer(session_id)) {
                let message = format!("Ban for {} seconds, reason: {}", duration.as_secs(), reason);
                self.peer_store.lock().ban_addr(
                    &peer.connected_addr,
                    duration.as_millis() as u64,
                    reason,
                );
```

**File:** network/src/network.rs (L643-656)
```rust
            ServiceError::ProtocolError {
                id,
                proto_id,
                error,
            } => {
                debug!("ProtocolError({}, {}) {}", id, proto_id, error);
                let message = format!("ProtocolError id={proto_id}");
                // Ban because misbehave of remote peer
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    id,
                    Duration::from_secs(300),
                    message,
                );
```

**File:** network/src/peer_store/types.rs (L152-156)
```rust
pub fn ip_to_network(ip: IpAddr) -> IpNetwork {
    match ip {
        IpAddr::V4(ipv4) => IpNetwork::V4(ipv4.into()),
        IpAddr::V6(ipv6) => IpNetwork::V6(ipv6.into()),
    }
```

**File:** network/src/peer_store/ban_list.rs (L48-58)
```rust
    fn is_ip_banned_until(&self, ip: IpAddr, now_ms: u64) -> bool {
        let ip_network = ip_to_network(ip);
        if let Some(banned_addr) = self.inner.get(&ip_network)
            && banned_addr.ban_until.gt(&now_ms)
        {
            return true;
        }

        self.inner.iter().any(|(ip_network, banned_addr)| {
            banned_addr.ban_until.gt(&now_ms) && ip_network.contains(ip)
        })
```
