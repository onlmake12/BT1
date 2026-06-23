### Title
Redundant `has_received` Check Before Signature Verification Enables Ban Evasion in Alert Relayer - (File: util/network-alert/src/alert_relayer.rs)

---

### Summary

In `AlertRelayer::received()`, the `has_received(alert_id)` deduplication check is evaluated **before** signature verification. Because active alert IDs are publicly broadcast to every connecting peer, any unprivileged P2P peer can craft alert messages carrying a known active alert ID with invalid or absent signatures. The node silently returns early without banning the sender. The outer `has_received` check is also structurally redundant: `Notifier::add()` performs the identical check internally, so the outer guard adds no correctness value while creating the ban-evasion window.

---

### Finding Description

**Root cause — check ordering in `AlertRelayer::received()`**

```
alert_relayer.rs  line 144  let alert_id = alert.as_reader().raw().id().into();
alert_relayer.rs  line 147  if self.notifier.lock().has_received(alert_id) { return; }   // ← early exit, no ban
alert_relayer.rs  line 151  if let Err(err) = self.verifier.verify_signatures(&alert) {
alert_relayer.rs  line 156      nc.ban_peer(peer_index, BAD_MESSAGE_BAN_TIME, ...);       // ← never reached for known IDs
``` [1](#0-0) 

When `has_received(alert_id)` returns `true`, the function returns at line 148 with no signature check and no peer ban. The signature-verification branch and the `nc.ban_peer(...)` call at line 156 are completely bypassed.

**Why the outer check is redundant**

`Notifier::add()` already contains the identical guard:

```rust
pub fn add(&mut self, alert: &Alert) {
    let alert_id = alert.raw().id().into();
    if self.has_received(alert_id) { return; }   // inner duplicate guard
    ...
}
``` [2](#0-1) 

The outer check in `alert_relayer.rs` is therefore redundant with the inner check in `notifier.rs`. Its only observable effect is to short-circuit signature verification for already-known alert IDs.

**How active alert IDs are disclosed**

On every new peer connection the `connected()` handler iterates `received_alerts()` and pushes each stored alert (including its ID) to the newly connected peer:

```rust
for alert in self.notifier.lock().received_alerts() {
    let alert_id: u32 = alert.as_reader().raw().id().into();
    nc.quick_send_message_to(peer_index, alert.as_bytes());
}
``` [3](#0-2) 

Any peer that connects learns all active alert IDs immediately, with zero privilege required.

**`has_received` checks both live and cancelled IDs**

```rust
pub fn has_received(&self, id: u32) -> bool {
    self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
}
``` [4](#0-3) 

Both live alerts and recently-cancelled alerts (held in the LRU `cancel_filter` of size 128) satisfy the early-return condition, widening the set of IDs an attacker can exploit.

---

### Impact Explanation

A malicious peer can send an unbounded stream of well-formed but invalid-signature alert messages using any known active alert ID. Each message passes the UTF-8 parse check (lines 106–119), hits the `has_received` early return, and exits without triggering `nc.ban_peer(...)`. The peer is never disconnected or penalised regardless of how many such messages it sends. This defeats the ban mechanism that is the only enforcement tool against peers that submit invalid alert signatures, allowing a persistent malicious peer to remain connected and continue sending other protocol messages without consequence.

---

### Likelihood Explanation

Exploitation requires only: (1) establishing a standard P2P connection to any CKB node, and (2) reading the alert IDs that the node immediately sends on connection. Both steps are available to any unprivileged external peer. No keys, credentials, or elevated access are needed. Active alerts are rare in practice, but when one exists the window is open for the entire alert lifetime.

---

### Recommendation

Move the `has_received` deduplication check to **after** signature verification, or remove the outer check entirely and rely on the identical guard already present inside `Notifier::add()`. The corrected order should be:

1. Parse and UTF-8 validate the message (existing).
2. Verify signatures → ban peer on failure (existing, but must run unconditionally).
3. Check `has_received` → return early if already seen (deduplication, no ban needed here).
4. Broadcast and add to notifier (existing).

---

### Proof of Concept

1. Connect to a CKB mainnet/testnet node over the alert P2P sub-protocol.
2. Receive the `connected()` push: the node sends all stored alerts, revealing their IDs (e.g., `alert_id = 0x2a`).
3. Construct a syntactically valid `packed::Alert` with `id = 0x2a`, valid UTF-8 fields, but with the `signatures` field empty or containing garbage bytes.
4. Send this message repeatedly to the node.
5. Observe: the node never calls `ban_peer` for this peer. The peer remains connected and un-penalised indefinitely, because line 147 returns early before reaching the ban path at line 156. [5](#0-4)

### Citations

**File:** util/network-alert/src/alert_relayer.rs (L87-95)
```rust
        self.clear_expired_alerts();
        for alert in self.notifier.lock().received_alerts() {
            let alert_id: u32 = alert.as_reader().raw().id().into();
            trace!("Send alert {} to peer {}", alert_id, peer_index);
            if let Err(err) = nc.quick_send_message_to(peer_index, alert.as_bytes()) {
                debug!("alert_relayer send alert when connected error: {:?}", err);
            }
        }
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L144-162)
```rust
        let alert_id = alert.as_reader().raw().id().into();
        trace!("ReceiveD alert {} from peer {}", alert_id, peer_index);
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
        // verify
        if let Err(err) = self.verifier.verify_signatures(&alert) {
            debug!(
                "An alert from peer {} with invalid signatures, error {:?}",
                peer_index, err
            );
            nc.ban_peer(
                peer_index,
                BAD_MESSAGE_BAN_TIME,
                String::from("send us an alert with invalid signatures"),
            );
            return;
        }
```

**File:** util/network-alert/src/notifier.rs (L93-98)
```rust
    pub fn add(&mut self, alert: &Alert) {
        let alert_id = alert.raw().id().into();
        let alert_cancel = alert.raw().cancel().into();
        if self.has_received(alert_id) {
            return;
        }
```

**File:** util/network-alert/src/notifier.rs (L147-149)
```rust
    pub fn has_received(&self, id: u32) -> bool {
        self.received_alerts.contains_key(&id) || self.cancel_filter.contains(&id)
    }
```
