### Title
Silent Discard of All Valid Peer Addresses Due to Missing Per-Address Error Handling in P2P Discovery Protocol — (File: `network/src/protocols/discovery/protocol.rs`)

---

### Summary

In `DiscoveryMessage::decode`, when processing a `Nodes` message, a single unparseable `Multiaddr` entry causes the **entire message to be silently dropped** via an early `?`-return. All valid peer addresses carried in the same message are discarded. An unprivileged peer can exploit this to prevent a victim node from learning about any peers advertised in a crafted `Nodes` message.

---

### Finding Description

The `DiscoveryMessage::decode` function processes incoming `Nodes` messages from P2P peers. For each node entry, it iterates over the list of addresses and attempts to parse each one as a `Multiaddr`: [1](#0-0) 

```rust
for node_reader in reader.items().iter() {
    let mut addresses = Vec::with_capacity(node_reader.addresses().len());
    for address_reader in node_reader.addresses().iter() {
        addresses
            .push(Multiaddr::try_from(address_reader.raw_data().to_vec()).ok()?)
    }
```

The expression `.ok()?` converts the `Result` from `Multiaddr::try_from` into an `Option`, then the `?` operator propagates `None` out of the enclosing `decode` function (which returns `Option<Self>`). If **any single address** in **any single node entry** fails to parse, the entire `decode` call returns `None` and the whole `Nodes` message is discarded.

The caller in the discovery handler treats a `None` decode result as a silent no-op — no ban, no warning, no partial processing: [2](#0-1) 

The entry point for this code is the `received` callback of the discovery `ServiceProtocol`, which calls `decode` on every inbound `Nodes` message from any connected peer. [3](#0-2) 

This is structurally identical to the reported ERC-20 vulnerability: just as a token returning `bytes32` instead of `string` caused the entire `sendToken` call to revert (discarding the whole bridging operation), here a single non-standard address encoding causes the entire peer-advertisement message to be discarded (losing all valid addresses it contained).

---

### Impact Explanation

A malicious peer sends a `Nodes` message containing N valid peer addresses and one deliberately malformed `Multiaddr` byte sequence. The victim node's `decode` returns `None` and discards all N valid addresses. The victim node never adds those peers to its peer store.

If the victim node is in a low-connectivity state (e.g., shortly after startup or after a network partition), and the attacker is one of its few connected peers, the attacker can continuously poison every `Nodes` response it sends. This degrades or blocks the victim's ability to discover new peers through the discovery protocol, potentially prolonging network isolation. The attack requires no privileges, no keys, and no majority hashpower — only a standard P2P connection.

---

### Likelihood Explanation

Any node that establishes a P2P connection to a victim can send `Nodes` messages. The discovery protocol actively solicits these messages via `GetNodes`. The crafted payload requires only inserting one byte sequence that fails `Multiaddr` parsing among otherwise valid entries. This is trivially constructable. The victim node does not ban or penalize the sender for a decode failure, so the attacker can repeat the attack indefinitely without consequence.

---

### Recommendation

Replace the early-exit `?` with per-address error handling so that invalid addresses are skipped and valid ones are retained:

```rust
for address_reader in node_reader.addresses().iter() {
    match Multiaddr::try_from(address_reader.raw_data().to_vec()) {
        Ok(addr) => addresses.push(addr),
        Err(_) => {
            // log and skip; do not discard the entire message
            debug!("Skipping unparseable address in Nodes message");
        }
    }
}
```

This mirrors the fix applied to the ERC-20 vault: wrap the fallible call, assign a default/skip on failure, and continue processing the remaining valid data. Optionally, consider banning peers that send a configurable threshold of malformed addresses, analogous to the existing `BAD_MESSAGE_BAN_TIME` logic used in the sync and relay protocols. [4](#0-3) 

---

### Proof of Concept

1. Establish a P2P connection to a victim CKB node.
2. Construct a molecule-encoded `DiscoveryMessage` of type `Nodes` containing:
   - One or more valid `Node` entries with legitimate `Multiaddr` addresses.
   - One `Node` entry whose `addresses` field contains a raw byte sequence that is not a valid `Multiaddr` (e.g., a zero-length byte string or a byte string with an unrecognized protocol prefix).
3. Send the message to the victim node on the discovery protocol channel.
4. Observe that the victim node's peer store does not gain any of the valid addresses from the message.
5. Repeat continuously. The victim node is never banned or penalized.

The decode path that drops the message is: [5](#0-4)

### Citations

**File:** network/src/protocols/discovery/protocol.rs (L95-96)
```rust
    pub fn decode(data: &[u8]) -> Option<Self> {
        let reader = packed::DiscoveryMessageReader::from_compatible_slice(data).ok()?;
```

**File:** network/src/protocols/discovery/protocol.rs (L136-142)
```rust
                let mut items = Vec::with_capacity(reader.items().len());
                for node_reader in reader.items().iter() {
                    let mut addresses = Vec::with_capacity(node_reader.addresses().len());
                    for address_reader in node_reader.addresses().iter() {
                        addresses
                            .push(Multiaddr::try_from(address_reader.raw_data().to_vec()).ok()?)
                    }
```

**File:** network/src/protocols/discovery/mod.rs (L19-22)
```rust
use self::{
    protocol::{decode, encode},
    state::RemoteAddress,
};
```

**File:** sync/src/relayer/mod.rs (L831-839)
```rust
                        nc.ban_peer(
                            peer_index,
                            BAD_MESSAGE_BAN_TIME,
                            String::from(
                                "send us a malformed message: \
                                 too many fields in CompactBlock",
                            ),
                        );
                        return;
```
