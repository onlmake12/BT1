The code is confirmed. Let me verify the frame-size configuration to understand the practical bound on per-message cost.

Now I have all the information needed. The frame size limits are critical:

- **Identify**: `max_frame_length = 2 KB` → at most ~82 entries per message
- **Discovery**: `max_frame_length = 512 KB` → potentially tens of thousands of `Multiaddr::try_from` calls per message

The code behavior is confirmed exactly as described. Let me write the report.

---

Audit Report

## Title
Unbounded Memory Allocation and Parsing Before Count Validation in `DiscoveryMessage::decode` and `IdentifyMessage::decode` — (`network/src/protocols/discovery/protocol.rs`, `network/src/protocols/identify/protocol.rs`)

## Summary
Both `DiscoveryMessage::decode` and `IdentifyMessage::decode` fully materialize all wire-encoded entries into heap-allocated `Multiaddr` objects before any count validation is applied. A malicious peer can craft a message up to the protocol's frame-size limit, forcing the receiving node to allocate and parse every entry before the count check rejects the message. The attack is repeatable at the cost of a TCP handshake per cycle and is reachable from any unprivileged peer.

## Finding Description

**`IdentifyMessage::decode` — `network/src/protocols/identify/protocol.rs` lines 71–79:**

```rust
let mut listen_addrs = Vec::with_capacity(reader.listen_addrs().len()); // attacker-controlled
for addr in reader.listen_addrs().iter() {
    match Multiaddr::try_from(addr.bytes().raw_data().to_vec()) { // parse every entry
        Ok(multi_addr) => { listen_addrs.push(multi_addr); }
        Err(err) => warn!(...),
    }
}
```

The count check (`MAX_ADDRS = 10`) is only applied in `process_listens` after `decode` returns, as the call sequence in `received` is: `decode` → `check_duplicate` → `received_identify` → `process_listens`. The Identify protocol's `max_frame_length` is 2 KB, which bounds the attack to approximately 82 entries per message — a factor of ~8× over the limit of 10.

**`DiscoveryMessage::decode` — `network/src/protocols/discovery/protocol.rs` lines 136–152:**

```rust
let mut items = Vec::with_capacity(reader.items().len()); // attacker-controlled
for node_reader in reader.items().iter() {
    let mut addresses = Vec::with_capacity(node_reader.addresses().len()); // attacker-controlled
    for address_reader in node_reader.addresses().iter() {
        addresses.push(Multiaddr::try_from(address_reader.raw_data().to_vec()).ok()?)
    }
    items.push(Node { addresses, flags })
}
```

`verify_nodes_message` (which enforces `MAX_ADDR_TO_SEND = 1000` items and `MAX_ADDRS = 3` addresses per item) is called only after `decode` returns in `received`. The Discovery protocol's `max_frame_length` is 512 KB. With minimal-size Molecule address entries (~11 bytes each), an attacker can pack approximately 47,000 `Multiaddr::try_from` calls into a single message — far exceeding the intended limits of 1,000 items × 3 addresses = 3,000 total.

**Existing guards are insufficient:** The `has_received` guard in `check_duplicate` is per-session and is only checked after `decode` completes. Reconnecting creates a new session with `has_received = false`, so the attacker gets a fresh decode pass on every reconnect.

## Impact Explanation

The Discovery protocol attack is the primary concern. A single crafted 512 KB `Nodes` message forces ~47,000 heap allocations and `Multiaddr::try_from` parse calls before the node disconnects. Repeated at the rate of TCP handshakes (potentially hundreds per second from a single attacker), this creates sustained CPU and heap pressure on the P2P layer. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The Identify protocol attack is secondary and more limited (2 KB frame → ~82 entries max), but the same structural flaw exists.

## Likelihood Explanation

Any peer that can establish a TCP connection to the node's P2P port can trigger this. No authentication, stake, or privileged role is required. The Discovery protocol is a core protocol opened for every peer session. The attack is repeatable at the cost of a TCP handshake per iteration, with no per-attempt cost to the attacker beyond the connection setup.

## Recommendation

Move the count guard into `decode` itself, before any allocation or iteration:

**For `IdentifyMessage::decode`:**
```rust
pub(crate) fn decode(data: &'a [u8]) -> Option<Self> {
    let reader = packed::IdentifyMessageReader::from_compatible_slice(data).ok()?;
    if reader.listen_addrs().len() > MAX_ADDRS {
        return None;
    }
    let mut listen_addrs = Vec::with_capacity(reader.listen_addrs().len());
    // ...
}
```

**For `DiscoveryMessage::decode` (Nodes branch):**
```rust
let items_reader = reader.items();
if items_reader.len() > MAX_ADDR_TO_SEND {
    return None;
}
let mut items = Vec::with_capacity(items_reader.len());
for node_reader in items_reader.iter() {
    if node_reader.addresses().len() > MAX_ADDRS {
        return None;
    }
    // ...
}
```

## Proof of Concept

**Discovery protocol (primary):**
1. Connect to a CKB node's P2P port and complete the Tentacle handshake.
2. Open the Discovery protocol sub-stream.
3. Craft a Molecule-encoded `DiscoveryMessage::Nodes` with `announce=false` and `items` containing the maximum number of `Node2` entries that fit within 512 KB, each carrying the maximum number of `BytesVec` address entries (each a minimal valid Molecule `Bytes` value, ~11 bytes).
4. Send the message. Observe the node allocating nested `Vec`s and calling `Multiaddr::try_from` for every entry before `verify_nodes_message` disconnects the session.
5. Reconnect immediately and repeat. Each cycle forces a fresh allocation-and-parse pass.

**Identify protocol (secondary):**
Same steps, opening the Identify protocol and sending a crafted `IdentifyMessage` with `listen_addrs` containing the maximum entries fitting within 2 KB (~82 entries vs the limit of 10). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** network/src/protocols/identify/protocol.rs (L71-79)
```rust
        let mut listen_addrs = Vec::with_capacity(reader.listen_addrs().len());
        for addr in reader.listen_addrs().iter() {
            match Multiaddr::try_from(addr.bytes().raw_data().to_vec()) {
                Ok(multi_addr) => {
                    listen_addrs.push(multi_addr);
                }
                Err(err) => warn!("failed to decode listen_addr to Multiaddr: {}", err),
            }
        }
```

**File:** network/src/protocols/identify/mod.rs (L134-136)
```rust
        if listens.len() > MAX_ADDRS {
            self.callback
                .misbehave(&info.session, Misbehavior::TooManyAddresses(listens.len()))
```

**File:** network/src/protocols/identify/mod.rs (L250-281)
```rust
    async fn received(&mut self, mut context: ProtocolContextMutRef<'_>, data: Bytes) {
        let session = context.session;
        match IdentifyMessage::decode(&data) {
            Some(message) => {
                trace!(
                    "IdentifyProtocol received, session: {:?}, listen_addrs: {:?}, observed_addr: {}",
                    context.session, message.listen_addrs, message.observed_addr
                );

                // Interrupt processing if error, avoid pollution
                if let MisbehaveResult::Disconnect = self.check_duplicate(&mut context) {
                    error!(
                        "Disconnect IdentifyProtocol session {:?} due to duplication.",
                        session
                    );
                    let _ = context.disconnect(session.id).await;
                    return;
                }
                if let MisbehaveResult::Disconnect = self
                    .callback
                    .received_identify(&mut context, message.identify)
                    .await
                {
                    error!(
                        "Disconnect IdentifyProtocol session {:?} due to invalid identify message.",
                        session,
                    );
                    let _ = context.disconnect(session.id).await;
                    return;
                }
                if let MisbehaveResult::Disconnect =
                    self.process_listens(&mut context, message.listen_addrs.clone())
```

**File:** network/src/protocols/discovery/protocol.rs (L136-152)
```rust
                let mut items = Vec::with_capacity(reader.items().len());
                for node_reader in reader.items().iter() {
                    let mut addresses = Vec::with_capacity(node_reader.addresses().len());
                    for address_reader in node_reader.addresses().iter() {
                        addresses
                            .push(Multiaddr::try_from(address_reader.raw_data().to_vec()).ok()?)
                    }
                    let flags = if node_reader.has_extra_fields() {
                        let node2 =
                            packed::Node2::from_compatible_slice(node_reader.as_slice()).ok()?;
                        let reader = node2.as_reader();
                        Flags::from_bits_truncate(reader.flags().into())
                    } else {
                        Flags::COMPATIBILITY
                    };
                    items.push(Node { addresses, flags })
                }
```

**File:** network/src/protocols/discovery/mod.rs (L170-178)
```rust
                    DiscoveryMessage::Nodes(nodes) => {
                        if let Some(misbehavior) = verify_nodes_message(&nodes)
                            && check(misbehavior)
                        {
                            if context.disconnect(session.id).await.is_err() {
                                debug!("Disconnect {:?} msg failed to send", session.id)
                            }
                            return;
                        }
```

**File:** network/src/protocols/discovery/mod.rs (L266-299)
```rust
fn verify_nodes_message(nodes: &Nodes) -> Option<Misbehavior> {
    let mut misbehavior = None;
    if nodes.announce {
        if nodes.items.len() > ANNOUNCE_THRESHOLD {
            warn!(
                "Number of nodes exceeds announce threshold {}",
                ANNOUNCE_THRESHOLD
            );
            misbehavior = Some(Misbehavior::TooManyItems {
                announce: nodes.announce,
                length: nodes.items.len(),
            });
        }
    } else if nodes.items.len() > MAX_ADDR_TO_SEND {
        warn!(
            "Too many items (announce=false) length={}",
            nodes.items.len()
        );
        misbehavior = Some(Misbehavior::TooManyItems {
            announce: nodes.announce,
            length: nodes.items.len(),
        });
    }

    if misbehavior.is_none() {
        for item in &nodes.items {
            if item.addresses.len() > MAX_ADDRS {
                misbehavior = Some(Misbehavior::TooManyAddresses(item.addresses.len()));
                break;
            }
        }
    }

    misbehavior
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
