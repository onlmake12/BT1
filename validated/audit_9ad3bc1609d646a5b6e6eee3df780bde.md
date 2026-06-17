The code is clear. Let me analyze the exact check in `process_incoming` and the public entrypoint.

The vulnerability is real and concrete. Here is the full analysis:

---

### Title
Unprivileged Relayer Can Permanently Skip Governance VAAs via Out-of-Order Sequence Relay — (`lazer/contracts/sui/sources/governance.move`)

### Summary
The `process_incoming` function in `governance.move` uses a weak sequence guard (`seen_sequence < sequence`) instead of strict sequential enforcement. Because `update_trusted_signer` (and `upgrade`) are `public` functions callable by any account, an unprivileged relayer who holds two valid Wormhole VAAs from the governance emitter can relay the higher-sequence VAA first, permanently making the lower-sequence VAA unprocessable.

### Finding Description

The guard in `process_incoming` is:

```move
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
self.seen_sequence = sequence;
``` [1](#0-0) 

This accepts **any** sequence strictly greater than `seen_sequence`, not just the next one. After processing VAA with sequence N+1, `seen_sequence` becomes N+1. Any subsequent attempt to process VAA N fails because `N+1 < N` is false.

The public entrypoint that any account can call:

```move
public fun update_trusted_signer(state: &mut State, vaa: VAA) {
    let current_cap = state.current_cap();
    let (header, mut parser) = state.unwrap_ptgm(&current_cap, vaa);
    ...
}
``` [2](#0-1) 

`unwrap_ptgm` calls `self.governance.process_incoming(vaa)` directly: [3](#0-2) 

There is no access control on `update_trusted_signer` — it is `public`, not `public(package)` or gated by a capability. Any address can submit any valid Wormhole VAA.

**Attack call sequence:**
1. Governance emitter issues VAA N (e.g., signer rotation) and VAA N+1 (any action). Both are publicly observable on the Wormhole guardian network.
2. Attacker calls `update_trusted_signer(state, vaa_N+1)` → passes (`0 < N+1`), `seen_sequence = N+1`.
3. Legitimate relayer calls `update_trusted_signer(state, vaa_N)` → aborts with `EOldSequenceNumber` (`N+1 < N` is false).
4. VAA N is permanently unprocessable with no on-chain recovery path.

### Impact Explanation
A governance action encoded in VAA N is silently and permanently dropped. If VAA N was a trusted signer rotation, the contract continues operating with a stale or expired signer set. The only recovery is for governance to re-issue the action as a new VAA with sequence > N+1, which requires off-chain coordination and awareness that the skip occurred — there is no on-chain signal that a VAA was skipped. [4](#0-3) 

### Likelihood Explanation
- Wormhole VAAs are publicly observable once signed by guardians; no privileged access is needed to obtain them.
- `update_trusted_signer` is `public` with no caller restriction.
- The attacker only needs to submit one transaction before the legitimate relayer, which is trivially achievable by monitoring the Wormhole guardian network and front-running.
- No profit motive is required; a griefing attacker suffices.

### Recommendation
Replace the weak sequence guard with strict sequential enforcement:

```move
assert!(self.seen_sequence + 1 == sequence, EOldSequenceNumber);
```

This ensures VAAs must be processed in the exact order they were emitted. If intentional skipping is ever needed, it should require an explicit governance action (e.g., a dedicated "skip sequence" VAA type), not be achievable by any unprivileged relayer.

### Proof of Concept
1. Create two mock VAAs from the authorized governance emitter with sequences 5 and 6.
2. Call `update_trusted_signer(state, vaa_6)` → succeeds; `seen_sequence = 6`.
3. Call `update_trusted_signer(state, vaa_5)` → aborts with `EOldSequenceNumber`.
4. Assert that the state reflects only the action from VAA 6, and VAA 5's action (e.g., a signer rotation) is permanently absent. [5](#0-4)

### Citations

**File:** lazer/contracts/sui/sources/governance.move (L55-66)
```text
public(package) fun process_incoming(
    self: &mut Governance,
    vaa: VAA
): vector<u8> {
    let sequence = vaa.sequence();
    let (chain_id, address, payload) = vaa.take_emitter_info_and_payload();
    assert!(self.chain_id == chain_id, EMismatchedEmitterChainID);
    assert!(self.address == address, EMismatchedAddress);
    assert!(self.seen_sequence < sequence, EOldSequenceNumber);
    self.seen_sequence = sequence;
    payload
}
```

**File:** lazer/contracts/sui/sources/actions.move (L61-63)
```text
public fun update_trusted_signer(state: &mut State, vaa: VAA) {
    let current_cap = state.current_cap();
    let (header, mut parser) = state.unwrap_ptgm(&current_cap, vaa);
```

**File:** lazer/contracts/sui/sources/state.move (L69-78)
```text
public(package) fun unwrap_ptgm(
    self: &mut State,
    _: &CurrentCap,
    vaa: VAA
): (GovernanceHeader, Parser) {
    let payload = self.governance.process_incoming(vaa);
    let mut parser = parser::new(payload);
    let header = governance::parse_header(&mut parser);
    (header, parser)
}
```

**File:** lazer/contracts/sui/sources/state.move (L104-145)
```text
/// Upsert a trusted signer's information or remove them.
/// - If the trusted signer pubkey already exists, the expires_at will be updated.
///   - If the expired_at is set to zero, the trusted signer will be removed.
/// - If the pubkey isn't found, it is added as a new trusted signer with the given expires_at.
public(package) fun update_trusted_signer(
    self: &mut State,
    _: &CurrentCap,
    pubkey: vector<u8>,
    expires_at: u64
) {
    assert!(
        pubkey.length() == secp256k1_compressed_pubkey_len(),
        EInvalidPubkeyLen
    );

    let mut maybe_idx = self.trusted_signers.find_index!(|signer|
        signer.public_key() == &pubkey
    );
    if (expires_at == 0) {
        if (maybe_idx.is_some()) {
            let idx = maybe_idx.extract();
            // Remove by swapping with last (order not preserved), discard
            // removed value
            self.trusted_signers.swap_remove(idx);
        } else {
            maybe_idx.destroy_none();
            abort ERemovedSignerNotFound
        };
        return
    };

    if (maybe_idx.is_some()) {
        let idx = maybe_idx.extract();
        let info_ref = &mut self.trusted_signers[idx];
        info_ref.expires_at = expires_at
    } else {
        maybe_idx.destroy_none();
        self.trusted_signers.push_back(
            TrustedSignerInfo { public_key: pubkey, expires_at }
        );
    }
}
```
