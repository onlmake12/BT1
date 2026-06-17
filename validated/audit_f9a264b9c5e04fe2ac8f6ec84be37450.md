Looking at the exact code in `governance.move` and `actions.move`:

### Title
Governance Sequence-Skip Allows Permanent Blocking of Intermediate Governance Actions — (`lazer/contracts/sui/sources/governance.move`)

---

### Summary

`governance::process_incoming` accepts any VAA whose sequence number is strictly greater than `seen_sequence`, then advances `seen_sequence` to that value. It does **not** enforce `sequence == seen_sequence + 1`. An unprivileged relayer who submits a legitimately guardian-signed governance VAA with sequence N >> S+1 permanently makes all governance VAAs at sequences S+1 … N-1 unprocessable.

---

### Finding Description

The sole replay/ordering guard in `process_incoming` is:

```move
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;
``` [1](#0-0) 

The check is a strict-less-than, not an equality to `seen_sequence + 1`. After `seen_sequence` is set to N, every subsequent call with any sequence in `(S, N)` aborts with `EOldSequenceNumber`.

The two public entrypoints that reach this path are:

```move
public fun update_trusted_signer(state: &mut State, vaa: VAA) { ... }
public fun upgrade(state: &mut State, vaa: VAA): UpgradeTicket { ... }
``` [2](#0-1) [3](#0-2) 

Both are `public fun` (not `entry fun`), callable by any Sui transaction that can supply a Wormhole-verified `VAA` object. Wormhole VAAs are public on-chain data; any relayer can fetch and submit them in any order.

The `Governance` struct stores only a single `seen_sequence: u64` — there is no bitmap, set, or queue of pending sequences:

```move
public struct Governance has copy, drop, store {
    chain_id: u16,
    address: ExternalAddress,
    seen_sequence: u64,
}
``` [4](#0-3) 

---

### Impact Explanation

If governance has emitted VAAs at sequences 1 … 100 and the contract has processed up to sequence 5, an attacker relays VAA #100 first. `seen_sequence` jumps to 100. VAAs #6–#99 — which may include approved contract upgrades or trusted-signer rotations — are permanently rejected. The voted governance outcome is never executed, matching the stated scope of "governance voting result manipulation that changes execution away from the voted outcome."

---

### Likelihood Explanation

- No privileged access is required. The attacker is any party who can submit a Sui transaction.
- Wormhole VAAs are public; a relayer can observe all emitted governance VAAs and deliberately submit the highest-sequence one first.
- The governance emitter will naturally accumulate many VAAs over time, widening the attack window.
- The attack is irreversible: once `seen_sequence = N`, no contract logic can lower it.

---

### Recommendation

Replace the strict-less-than check with an exact-next-sequence check:

```move
assert!(self.seen_sequence + 1 == sequence, EUnexpectedSequenceNumber);
self.seen_sequence = sequence;
``` [1](#0-0) 

This enforces that governance VAAs are processed in the exact order they were emitted, preventing any skip.

---

### Proof of Concept

1. Deploy the contract; `seen_sequence = 0`.
2. Obtain two valid guardian-signed governance VAAs from the correct emitter: one at sequence 1 (e.g., `UpdateTrustedSigner` for key A), one at sequence 1000 (e.g., `UpdateTrustedSigner` for key B).
3. Call `actions::update_trusted_signer(state, vaa_seq_1000)`.
   - `process_incoming`: `0 < 1000` → passes; `seen_sequence = 1000`.
   - Key B is added.
4. Call `actions::update_trusted_signer(state, vaa_seq_1)`.
   - `process_incoming`: `1000 < 1` → **aborts with `EOldSequenceNumber`**.
5. The governance action at sequence 1 (adding key A) is permanently blocked.

### Citations

**File:** lazer/contracts/sui/sources/governance.move (L34-38)
```text
public struct Governance has copy, drop, store {
    chain_id: u16,
    address: ExternalAddress,
    seen_sequence: u64,
}
```

**File:** lazer/contracts/sui/sources/governance.move (L63-64)
```text
    assert!(self.seen_sequence < sequence, EOldSequenceNumber);
    self.seen_sequence = sequence;
```

**File:** lazer/contracts/sui/sources/actions.move (L42-53)
```text
public fun upgrade(state: &mut State, vaa: VAA): UpgradeTicket {
    let current_cap = state.current_cap();
    let (header, mut parser) = state.unwrap_ptgm(&current_cap, vaa);
    assert!(header.is_upgrade_lazer_contract(), ENotUpgradeLazerContract);

    let version = parser.take_u64_be();
    assert!(version == meta::version() + 1, EInvalidUpgradeVersion);

    let digest = parser.take_bytes(32);
    parser.destroy_empty();
    state.authorize_upgrade(&current_cap, digest)
}
```

**File:** lazer/contracts/sui/sources/actions.move (L61-70)
```text
public fun update_trusted_signer(state: &mut State, vaa: VAA) {
    let current_cap = state.current_cap();
    let (header, mut parser) = state.unwrap_ptgm(&current_cap, vaa);
    assert!(header.is_update_trusted_signer(), ENotUpdateTrustedSigner);

    let public_key = parser.take_bytes(secp256k1_compressed_pubkey_len());
    let expires_at = parser.take_u64_be();
    parser.destroy_empty();
    state.update_trusted_signer(&current_cap, public_key, expires_at);
}
```
