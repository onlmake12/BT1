All code references in the claim check out against the actual source. Let me confirm the key facts:

1. `received()` has no `notice_until` check — confirmed at lines 98–179 of `alert_relayer.rs`
2. `clear_expired_alerts()` is only called in `connected()` (line 87), never in `received()` — confirmed
3. `clear_expired_alerts()` does not tombstone evicted IDs into `cancel_filter` — confirmed at `notifier.rs` lines 135–144
4. `has_received()` checks only `received_alerts` and `cancel_filter` — confirmed at line 147–149, meaning evicted-but-not-tombstoned IDs return `false`, bypassing deduplication
5. The RPC path does check `notice_until` (lines 104–110 of `alert.rs`) but the P2P path does not

The exploit path is fully reachable by an unprivileged peer, all cited line numbers are accurate, and the re-injection loop after cleanup is a real consequence of the missing tombstone.

---

Audit Report

## Title
Expired Alert Accepted and Re-broadcast via P2P Without `notice_until` Validation — (`util/network-alert/src/alert_relayer.rs`)

## Summary
`AlertRelayer::received` accepts and re-broadcasts network alerts over P2P without checking whether the alert's `notice_until` deadline has passed. Any unprivileged peer holding a historically valid signed alert can replay it after expiry, causing every receiving node to store it, fire `notify_network_alert`, and re-broadcast it to all connected peers. Because `clear_expired_alerts` evicts expired IDs without tombstoning them into `cancel_filter`, the deduplication guard is reset on each cleanup cycle, enabling a persistent spam loop.

## Finding Description
`AlertRelayer::received` (`alert_relayer.rs` L98–179) parses the alert, checks `has_received` (L147), verifies signatures (L151), then immediately broadcasts and calls `notifier.lock().add(&alert)` (L178) — with no check on `notice_until` at any point. The RPC path (`rpc/src/module/alert.rs` L104–110) correctly rejects expired alerts, but this guard is entirely absent from the P2P ingestion path.

`clear_expired_alerts` (`alert_relayer.rs` L58–61) is invoked only from `connected()` (L87), never from `received()`. It delegates to `Notifier::clear_expired_alerts` (`notifier.rs` L135–144), which removes expired IDs from `received_alerts` and `noticed_alerts` using `retain`, but never inserts them into `cancel_filter`. As a result, `has_received` (`notifier.rs` L147–149) returns `false` for any previously-seen-then-expired alert ID, because neither `received_alerts` nor `cancel_filter` contains it after cleanup. The deduplication guard at `alert_relayer.rs` L147 is therefore bypassed, and the same expired alert can be re-injected after every cleanup cycle.

`Notifier::add` (`notifier.rs` L93–122) also performs no expiry check; it unconditionally inserts into `received_alerts` (L104) and calls `notify_controller.notify_network_alert` (L115) for version-effective alerts.

## Impact Explanation
An attacker who replays an expired signed alert causes every receiving node to re-broadcast it to all connected peers, producing O(n) network-wide amplification per injection. Because the cleanup cycle resets deduplication state without tombstoning, the attacker can repeat the injection indefinitely with a single P2P connection and a single historical alert. This constitutes a low-cost, repeatable amplification attack capable of causing CKB network congestion — matching the **High** impact class (10001–15000 points): *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The attack requires only an unprivileged P2P connection and any historically broadcast signed alert (publicly observable from network traffic or on-chain data). No key material, special privilege, or majority hash power is needed. The replay is trivially repeatable: re-inject after each `connected` event triggers `clear_expired_alerts` on any node in the network. The barrier to exploitation is extremely low.

## Recommendation
Add an expiry check at the top of `AlertRelayer::received`, immediately after successful parsing and before signature verification:

```rust
let now_ms = ckb_systemtime::unix_time_as_millis();
let notice_until: u64 = alert.as_reader().raw().notice_until().into();
if notice_until < now_ms {
    return; // silently drop; optionally ban peer
}
```

Additionally, `Notifier::clear_expired_alerts` should tombstone evicted IDs into `cancel_filter` so that `has_received` correctly deduplicates re-injected expired alerts:

```rust
pub fn clear_expired_alerts(&mut self, now: u64) {
    self.received_alerts.retain(|id, alert| {
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until <= now {
            self.cancel_filter.put(*id, ());
            false
        } else {
            true
        }
    });
    // ... noticed_alerts retain unchanged
}
```

## Proof of Concept
1. Obtain any past CKB mainnet/testnet alert with valid 2-of-4 signatures whose `notice_until` has already passed (e.g., from historical network captures or public sources).
2. Connect to a live CKB node as a P2P peer on the Alert protocol (`SupportProtocols::Alert`).
3. Send the raw alert bytes.
4. Observe: the node passes `has_received` (returns `false` for expired/unknown ID), passes signature verification, calls `quick_filter_broadcast` to all connected peers, and calls `notifier.add()` which fires `notify_network_alert`.
5. Wait for any new peer to connect to the target node (triggering `clear_expired_alerts` without tombstoning).
6. Re-send the same alert bytes — the node accepts and re-broadcasts again, confirming the persistent loop.