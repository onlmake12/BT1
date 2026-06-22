### Title
Unbounded Memory Allocation in `IdentifyMessage::decode` Before Count Validation — (`network/src/protocols/identify/protocol.rs`)

### Summary
In `IdentifyMessage::decode`, a `Vec` is allocated with full capacity and all `listen_addrs` entries are parsed into `Multiaddr` objects before the `MAX_ADDRS` count check is applied. A malicious peer can craft an `IdentifyMessage` with many `listen_addrs` entries (up to the protocol frame-size limit), forcing the receiving node to allocate memory and execute `Multiaddr::try_from` for every entry before the count check in `process_listens` rejects the message. This is directly analogous to the reported pattern: data is fully materialized into memory before it is validated and discarded.

### Finding Description

**Root cause — `IdentifyMessage::decode`:**

```rust
// network/src/protocols/identify/protocol.rs  lines 65-86
pub(crate) fn decode(data: &'a [u8]) -> Option<Self> {
    let reader = packed::IdentifyMessageReader::from_compatible_slice(data).ok()?;
    let identify = reader.identify().raw_data();
    let observed_addr =
        Multiaddr::try_from(reader.observed_addr().bytes().raw_data().to_vec()).ok()?;
    let mut listen_addrs = Vec::with_capacity(reader.listen_addrs().len()); // ← attacker-controlled capacity
    for addr in reader.listen_addrs().iter() {
        match Multiaddr::try_from(addr.bytes().raw_data().to_vec()) {  // ← parse every entry
            Ok(multi_addr) => { listen_addrs.push(multi_addr); }
            Err(err) => warn!("failed to decode listen_addr to Multiaddr: {}", err),
        }
    }
    Some(IdentifyMessage { identify, observed_addr, listen_addrs })
}
```

`reader.listen_addrs().len()` is derived directly from the Molecule offset table in the wire bytes. A malicious peer controls this value up to the protocol's `max_frame_length`. The `Vec::with_capacity` call and the full iteration (including heap-allocating one `Multiaddr` per entry) happen unconditionally inside `decode`.

**The count check is applied only after decode returns**, inside `process_listens`:

```rust
// network/src/protocols/identify/mod.rs  lines 134-136
if listens.len() > MAX_ADDRS {   // MAX_ADDRS = 10
    self.callback.misbehave(...)
```

The call sequence in `received` is:
1. `IdentifyMessage::decode(&data)` — full allocation + parsing of all addresses
2. `check_duplicate` — duplicate session guard
3. `received_identify` — identify-bytes check
4. `process_listens` — **count check, too late** [1](#0-0) [2](#0-1) [3](#0-2) 

**Same pattern in `DiscoveryMessage::decode`:**

```rust
// network/src/protocols/discovery/protocol.rs  lines 136-152
let mut items = Vec::with_capacity(reader.items().len());   // ← attacker-controlled
for node_reader in reader.items().iter() {
    let mut addresses = Vec::with_capacity(node_reader.addresses().len()); // ← attacker-controlled
    for address_reader in node_reader.addresses().iter() {
        addresses.push(Multiaddr::try_from(address_reader.raw_data().to_vec()).ok()?)
    }
    items.push(Node { addresses, flags })
}
```

`verify_nodes_message` (which enforces `ANNOUNCE_THRESHOLD = 10` and `MAX_ADDR_TO_SEND = 1000`) is called only after `decode` returns in `received`. [4](#0-3) [5](#0-4) [6](#0-5) 

### Impact Explanation

A malicious peer connects to any CKB node, sends one crafted `IdentifyMessage` (or `DiscoveryMessage::Nodes`) containing the maximum number of `listen_addrs` entries that fit within the protocol frame limit. The node:

1. Allocates a `Vec` with capacity proportional to the entry count.
2. Calls `Multiaddr::try_from(addr.bytes().raw_data().to_vec())` for every entry — each call heap-allocates a new `Multiaddr`.
3. Only then checks the count and disconnects.

The attacker repeats this by reconnecting (the `has_received` guard is per-session). The result is sustained CPU and heap pressure on the victim node. Because the identify protocol is opened for every inbound and outbound peer connection, this is reachable from any unprivileged network peer. The impact is service degradation / resource exhaustion of the P2P layer.

### Likelihood Explanation

Any peer that can establish a TCP connection to the node can trigger this. No authentication, no stake, no privileged role is required. The identify protocol is mandatory and runs on every new session. The attack is repeatable at the cost of a TCP handshake per iteration.

### Recommendation

Move the count guard into `decode` itself, before the allocation and iteration:

```rust
pub(crate) fn decode(data: &'a [u8]) -> Option<Self> {
    let reader = packed::IdentifyMessageReader::from_compatible_slice(data).ok()?;
    // Reject before allocating or parsing any address
    if reader.listen_addrs().len() > MAX_ADDRS {
        return None;
    }
    let mut listen_addrs = Vec::with_capacity(reader.listen_addrs().len());
    ...
}
```

Apply the same early-exit pattern in `DiscoveryMessage::decode` for both `items.len()` and `node.addresses().len()` before entering the nested loops.

### Proof of Concept

1. Connect to a CKB node's P2P port.
2. Complete the Tentacle handshake and open the Identify protocol.
3. Send a crafted `IdentifyMessage` (Molecule-encoded) with `listen_addrs` containing the maximum number of `Address` entries that fit within the protocol's `max_frame_length` (each entry can be a minimal valid Molecule `Address` table, ~12 bytes).
4. Observe the node allocating a `Vec` of that size and calling `Multiaddr::try_from` for every entry before disconnecting.
5. Reconnect immediately and repeat; each cycle forces a fresh allocation-and-parse pass on the victim node.

The same steps apply to the Discovery protocol by sending a `Nodes` message with `announce=false` and `items.len()` exceeding `MAX_ADDR_TO_SEND`, each item carrying `addresses.len()` exceeding `MAX_ADDRS`. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** network/src/protocols/identify/protocol.rs (L65-86)
```rust
    pub(crate) fn decode(data: &'a [u8]) -> Option<Self> {
        let reader = packed::IdentifyMessageReader::from_compatible_slice(data).ok()?;

        let identify = reader.identify().raw_data();
        let observed_addr =
            Multiaddr::try_from(reader.observed_addr().bytes().raw_data().to_vec()).ok()?;
        let mut listen_addrs = Vec::with_capacity(reader.listen_addrs().len());
        for addr in reader.listen_addrs().iter() {
            match Multiaddr::try_from(addr.bytes().raw_data().to_vec()) {
                Ok(multi_addr) => {
                    listen_addrs.push(multi_addr);
                }
                Err(err) => warn!("failed to decode listen_addr to Multiaddr: {}", err),
            }
        }

        Some(IdentifyMessage {
            identify,
            observed_addr,
            listen_addrs,
        })
    }
```

**File:** network/src/protocols/identify/mod.rs (L24-30)
```rust
const MAX_RETURN_LISTEN_ADDRS: usize = 10;
const BAN_ON_NOT_SAME_NET: Duration = Duration::from_secs(5 * 60);
const CHECK_TIMEOUT_TOKEN: u64 = 100;
// Check timeout interval (seconds)
const CHECK_TIMEOUT_INTERVAL: u64 = 1;
const DEFAULT_TIMEOUT: u64 = 8;
const MAX_ADDRS: usize = 10;
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

**File:** network/src/protocols/discovery/mod.rs (L30-34)
```rust
const ANNOUNCE_THRESHOLD: usize = 10;
// The maximum number of new addresses to accumulate before announcing.
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
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
