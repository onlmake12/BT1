Audit Report

## Title
Unverified Caller-Supplied `from` Peer ID in Hole-Punching `ConnectionRequest`/`ConnectionSync` Allows State Poisoning and Forced Outbound Connections - (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

The CKB hole-punching protocol accepts the `from` field in `ConnectionRequest` and `ConnectionSync` messages entirely from the message payload, with no verification that it matches the actual peer ID of the session that delivered the message. Any connected peer can forge a `ConnectionRequest` claiming to originate from an arbitrary victim peer ID, poison the target node's `pending_delivered` state with attacker-controlled listen addresses, and then trigger outbound TCP connections to attacker-chosen endpoints via a forged `ConnectionSync`. This can be used to exhaust connection resources and crash a CKB node, or to establish unsolicited P2P sessions.

## Finding Description

**Root cause — unverified `from` in `ConnectionRequest`**

`RequestContent::try_from` parses `from` directly from the message bytes: [1](#0-0) 

No check is ever made that `content.from` equals the peer ID of the actual session (`self.peer`) that delivered the message. The peer registry is consulted only for routing (to look up `to_peer_id`), never to validate the sender's identity.

When `self_peer_id == &content.to`, `execute()` calls `respond_delivered` with the attacker-supplied `from_peer_id`: [2](#0-1) 

`respond_delivered` then inserts the attacker-controlled listen addresses into `pending_delivered` keyed by the forged peer ID: [3](#0-2) 

**Root cause — unverified `from` in `ConnectionSync`**

`SyncContent::try_from` has the identical defect: [4](#0-3) 

When the target node processes a `ConnectionSync`, it looks up `pending_delivered` using the attacker-supplied `content.from`: [5](#0-4) 

It then spawns `try_nat_traversal` tasks for every address in the poisoned entry, and on success calls `control.raw_session(...)` to establish a full P2P session: [6](#0-5) 

**Why existing guards fail**

- **`HOLE_PUNCHING_INTERVAL` check**: Only prevents re-poisoning the same `from_peer_id` within 2 minutes. The attacker can use distinct forged `from` peer IDs to bypass this entirely.
- **`forward_rate_limiter`**: Keyed on `(content.from, content.to, msg_item_id)` — all three values are attacker-controlled. Varying `content.from` or `msg_item_id` bypasses the limiter. [7](#0-6) 

- **Session `rate_limiter`**: Keyed on `(session_id, item_id)` at 30 req/s — limits throughput but does not prevent the attack.
- **`inflight_requests` guard**: Only applies in `ConnectionRequestDeliveredProcess` when the current node is the `from` originator. It does not protect the `to` target node from having its `pending_delivered` state poisoned via a forged `ConnectionRequest`. [8](#0-7) 

## Impact Explanation

**High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

A single connected attacker can, at up to 30 messages/second (session rate limit), forge `ConnectionRequest` messages with distinct `from` peer IDs. Each one causes the target node to:
1. Store a new entry in `pending_delivered`.
2. Initiate an outbound TCP connection attempt to an attacker-chosen address upon receiving the paired `ConnectionSync`.

Sustained at the permitted rate, this exhausts the target node's file descriptors and outbound connection pool, crashing the node. Additionally, a successful NAT traversal to the attacker's endpoint results in `raw_session` being called, adding the attacker as a new inbound session and bypassing normal peer discovery and connection limits. The attack also poisons `pending_delivered` for legitimate peer IDs, causing DoS of real hole-punching flows.

## Likelihood Explanation

**High.** The attacker only needs to be a connected peer — reachable by any unprivileged external party. No keys, credentials, or special privileges are required. The forged `from` field is a plain byte sequence; no cryptographic material is needed. The hole-punching protocol is active whenever the node has outbound connections and NAT traversal is needed. The attack is repeatable and automatable.

## Recommendation

After parsing `content`, verify that `content.from` matches the actual peer ID of the delivering session. The peer ID can be resolved from the peer registry using `self.peer` (the `PeerIndex`):

```rust
// In ConnectionRequestProcess::execute(), after parsing content:
let actual_peer_id = self.protocol.network_state
    .peer_registry
    .read()
    .get_peer(self.peer)
    .map(|p| p.peer_id.clone());

if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match actual sender");
}
```

Apply the identical check in `ConnectionSyncProcess::execute()`. This ensures the `from` field is always the verified identity of the actual session, not a caller-supplied value.

## Proof of Concept

```
Precondition: Attacker (peer A) is directly connected to target node C.

Step 1 — Forge ConnectionRequest:
  A → C:
    ConnectionRequest {
      from: <arbitrary_peer_B_id>,   // forged; not A's real peer ID
      to:   <peer_C_id>,
      listen_addrs: [<attacker_ip:port>],
      max_hops: 6,
      route: []
    }

Step 2 — C processes request (connection_request.rs:145-147):
  self_peer_id == content.to → respond_delivered(peer_B_id, C, [attacker_ip:port])
  C stores: pending_delivered[peer_B_id] = ([attacker_ip:port], now)
  C sends ConnectionRequestDelivered back to A (self.peer)

Step 3 — Forge ConnectionSync:
  A → C:
    ConnectionSync {
      from: <arbitrary_peer_B_id>,   // same forged identity
      to:   <peer_C_id>,
      route: [],
      sync_route: []
    }

Step 4 — C processes sync (connection_sync.rs:111-115):
  listens_info = pending_delivered[peer_B_id] = [attacker_ip:port]
  C calls try_nat_traversal(bind_addr, attacker_ip:port)
  → outbound TCP SYN to attacker_ip:port
  → on success: control.raw_session(...) establishes P2P session with attacker

Repeat Steps 1–4 with distinct peer_B_id values (up to 30/s per session
rate limiter) to exhaust file descriptors and crash the node.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequest",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequest");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L42-44)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L154-160)
```rust
                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-175)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
```
