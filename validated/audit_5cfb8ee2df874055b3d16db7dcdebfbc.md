All six code citations verify exactly against the source. The vulnerability is real and all claims check out.

Audit Report

## Title
Missing Peer Ban for Malformed Disconnect Messages Enables Inbound Slot Exhaustion via Reconnect Loop — (`network/src/protocols/disconnect_message.rs`)

## Summary

The `DisconnectMessageProtocol::received` handler disconnects peers that send non-UTF-8 payloads but never calls `ban_session` or `ban_addr`. Because no ban entry is written, the attacker's address always passes the `is_addr_banned` check in `accept_peer`, allowing unlimited reconnects. When inbound slots are full, each reconnect triggers `try_evict_inbound_peer`, continuously displacing honest peers at negligible cost to the attacker.

## Finding Description

In `network/src/protocols/disconnect_message.rs` L32–38, the `else` branch for a failed `String::from_utf8` logs a debug message and falls through to `context.disconnect(session_id)` at L39–41. There are zero calls to `ban_session`, `ban_addr`, or any equivalent anywhere in the file.

The ban infrastructure is fully present. `NetworkState::ban_session` at `network/src/network.rs` L241–281 calls `peer_store.ban_addr(...)`, and `accept_peer` at `network/src/peer_registry.rs` L109–111 checks `peer_store.is_addr_banned(&remote_addr)` before admitting any non-whitelisted peer. The same `ban_session` call is already used for `ProtocolError` at `network/src/network.rs` L651–656.

Because no ban is written, `is_addr_banned` always returns false for the attacker's address. When `non_whitelist_inbound >= max_inbound`, `try_evict_inbound_peer` is called at `peer_registry.rs` L117–118 to make room. On `SessionClose`, `remove_peer` and `remove_disconnected_peer` at `network/src/network.rs` L800–813 clear the attacker's peer ID from the registry entirely, allowing immediate reconnect with the same keypair — no key rotation required.

## Impact Explanation

An unprivileged attacker with a single IP can continuously occupy and vacate one inbound slot, triggering `try_evict_inbound_peer` on each reconnect when slots are full. With multiple source IPs the attack scales to fill all inbound slots. This degrades the victim node's peer connectivity and disrupts block and transaction propagation — matching **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

The attack requires only a TCP connection and the ability to send two arbitrary bytes of invalid UTF-8. No special privileges, leaked keys, or hashpower are needed. The attacker does not need to rotate keypairs, as the session close path fully removes their peer ID from the registry before they reconnect. The secio handshake adds per-connection latency but does not prevent the attack at any realistic reconnect rate.

## Recommendation

In the `else` branch of `received` (lines 32–38 of `disconnect_message.rs`), call `self.0.ban_session(p2p_control, session_id, Duration::from_secs(300), "malformed disconnect message".to_string())` before or instead of the plain `context.disconnect`. The `ban_session` method already exists in `NetworkState` and is used for `ProtocolError` in `handle_error`. The `DisconnectMessageProtocol` struct already holds an `Arc<NetworkState>` (`self.0`), so the call requires only passing a `ServiceControl` reference available via `context.control()`.

## Proof of Concept

1. Start a CKB node with `max_inbound = N`.
2. Fill all N inbound slots with honest peers (e.g., via N controlled connections that stay open).
3. From an attacker process: connect to the node, open protocol `/ckb/disconnectmsg`, send `[0xFF, 0xFE]` (invalid UTF-8).
4. Observe the node disconnects the attacker session; call `get_banned_addrs` RPC and assert the attacker's address is **absent**.
5. Immediately reconnect from the same address with the same keypair; observe that `try_evict_inbound_peer` runs and one honest peer is disconnected (confirmed via `SessionClose` log for an honest peer's session ID).
6. Repeat steps 3–5 in a tight loop; confirm honest peers are continuously displaced while the attacker's address never appears in the ban list.