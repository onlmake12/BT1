### Title
SignatureSet Account Poisoning via Front-Running `init_if_needed` — (`target_chains/solana/programs/core-bridge/src/legacy/processor/verify_signatures.rs`)

### Summary

The `VerifySignatures` instruction uses `init_if_needed` on a free-keypair `signature_set` account and determines "already initialized" solely by whether any `sig_verify_successes` entry is `true`. An attacker who observes the victim's keypair can pre-initialize that account with a different `message_hash` or `guardian_set_index`, causing every subsequent legitimate `VerifySignatures` call against that keypair to revert with `MessageMismatch` or `GuardianSetMismatch`.

---

### Finding Description

**Root cause — `is_initialized()` is content-based, not creation-based.** [1](#0-0) 

`is_initialized()` returns `true` if any element of `sig_verify_successes` is `true`. It says nothing about *who* wrote those values or *which* VAA they correspond to.

**The account is a free keypair, not a PDA.** [2](#0-1) 

There are no `seeds` or `bump` constraints on `signature_set`. Any party can pass any writable account at that address.

**The branching logic trusts whatever was written first.** [3](#0-2) 

- If `is_initialized()` is `false` → the account is written with the *current* call's `message_hash` and `guardian_set_index`.
- If `is_initialized()` is `true` → the stored values are compared against the current call's values; a mismatch reverts.

**Attack path:**

1. Attacker observes the keypair `K` the victim will use for `signature_set`.
2. Attacker submits `VerifySignatures` with `signature_set = K` and a secp256k1 instruction carrying valid guardian signatures for a *previously observed* VAA (message hash `H_evil`). This is trivially available from any past on-chain VAA.
3. The account is created with `message_hash = H_evil` and at least one `sig_verify_successes[i] = true`.
4. Victim submits `VerifySignatures` for the real VAA (message hash `H_real`).
5. `is_initialized()` → `true`; `require_eq!(message_hash, signature_set.message_hash)` → **`MessageMismatch`** revert. [4](#0-3) 

The same attack works with a mismatched `guardian_set_index` by passing an older (still-active) guardian set. [5](#0-4) 

---

### Impact Explanation

`PostVaa` derives the `posted_vaa` PDA from `signature_set.message_hash`. [6](#0-5) 

A poisoned `signature_set` means `PostVaa` can never be called successfully for that keypair. The victim must generate a new keypair and restart the multi-transaction `VerifySignatures` flow. The attacker can repeat the front-run for every new keypair, creating a sustained DoS against any VAA relay that depends on this legacy path.

---

### Likelihood Explanation

- **Attacker material**: Any previously relayed VAA provides valid guardian signatures for a different message hash — no key compromise needed.
- **Keypair prediction**: The keypair is generated client-side and broadcast in the transaction. On Solana, RPC nodes expose pending transactions; a watching attacker can extract the `signature_set` pubkey and submit a competing transaction in the same or next slot.
- **Cost**: One transaction with a secp256k1 instruction and a small account rent deposit.

---

### Recommendation

Replace the free-keypair `signature_set` with a **PDA derived from the message hash and guardian set index**:

```rust
seeds = [
    b"signature-set",
    message_hash.as_ref(),
    guardian_set_index.to_be_bytes().as_ref(),
],
bump,
```

This makes the account address deterministic and unique per `(message_hash, guardian_set_index)` pair, eliminating the ability for a third party to pre-create it with different content. The `init_if_needed` pattern then becomes safe because the PDA can only ever hold data consistent with its derivation inputs.

---

### Proof of Concept

```rust
// 1. Attacker has previously seen VAA_A with message_hash_A and valid guardian sigs.
// 2. Victim generates keypair K for their signature_set.
// 3. Attacker submits:
let attacker_tx = VerifySignatures {
    payer: attacker,
    guardian_set: current_guardian_set,   // valid, active
    signature_set: K,                      // victim's keypair
    // secp256k1 instruction: valid sigs for message_hash_A (any past VAA)
};
// -> SignatureSet at K is now: { message_hash: hash_A, sig_verify_successes: [true, ...] }

// 4. Victim submits:
let victim_tx = VerifySignatures {
    payer: victim,
    guardian_set: current_guardian_set,
    signature_set: K,
    // secp256k1 instruction: valid sigs for message_hash_B (the real VAA)
};
// -> is_initialized() == true
// -> require_eq!(hash_B, hash_A) => ERROR: MessageMismatch
``` [7](#0-6)

### Citations

**File:** target_chains/solana/programs/core-bridge/src/legacy/state/signature_set.rs (L50-52)
```rust
    pub fn is_initialized(&self) -> bool {
        self.sig_verify_successes.iter().any(|&value| value)
    }
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/verify_signatures.rs (L52-57)
```rust
    #[account(
        init_if_needed,
        payer = payer,
        space = SignatureSet::compute_size(guardian_set.inner().keys.len())
    )]
    signature_set: Account<'info, SignatureSet>,
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/verify_signatures.rs (L161-187)
```rust
    if signature_set.is_initialized() {
        // Otherwise, verify that the guardian set index is what we expect from
        // the last time we wrote to the signature set account.
        require_eq!(
            guardian_set.index,
            signature_set.guardian_set_index,
            CoreBridgeError::GuardianSetMismatch
        );

        // And verify that the message hash is the same as the one already encoded in the signature
        // set.
        require_eq!(
            message_hash,
            signature_set.message_hash,
            CoreBridgeError::MessageMismatch
        );
    } else {
        // We are assuming that the signature set has not been "initialized" if there is no
        // indication of verified signatures (via `sig_verify_successes`) written to this account
        // yet. If we reach this condition, we set the message hash and guardian set index because
        // we are assuming that the account is created with this instruction invocation.
        signature_set.set_inner(SignatureSet {
            sig_verify_successes: vec![false; guardians.len()],
            message_hash,
            guardian_set_index: guardian_set.index,
        });
    }
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_vaa.rs (L56-65)
```rust
    #[account(
        init,
        payer = payer,
        space = PostedVaaV1::compute_size(args.payload.len()),
        seeds = [
            PostedVaaV1::SEED_PREFIX,
            signature_set.message_hash.as_ref()
        ],
        bump,
    )]
```
