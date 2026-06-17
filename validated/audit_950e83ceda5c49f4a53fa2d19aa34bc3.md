The code is fully readable. Here is my analysis:

---

### Title
Unprivileged SOL Drain from `SignatureSet` Accounts via `close_signature_set` — (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_signature_set.rs`)

### Summary

`CloseSignatureSet` requires `sol_destination` to be a `Signer` but imposes **no constraint** that it matches the original payer who funded the `signature_set`. Any unprivileged caller can sign a transaction with their own key as `sol_destination`, supply a valid `posted_vaa` + `signature_set` pair, and receive all lamports from the closed account.

### Finding Description

The `CloseSignatureSet` account context is:

```rust
#[derive(Accounts)]
pub struct CloseSignatureSet<'info> {
    #[account(mut)]
    sol_destination: Signer<'info>,          // ← only requires Signer, no payer-match

    #[account(
        seeds = [PostedVaaV1::SEED_PREFIX, posted_vaa.message_hash().as_ref()],
        bump,
        has_one = signature_set
    )]
    posted_vaa: Account<'info, LegacyAnchorized<PostedVaaV1>>,

    #[account(
        mut,
        close = sol_destination              // ← all lamports go to sol_destination
    )]
    signature_set: Account<'info, AccountVariant<SignatureSet>>,
}
``` [1](#0-0) 

The `SignatureSet` struct stores only `sig_verify_successes`, `message_hash`, and `guardian_set_index` — **no payer field** is recorded at creation time: [2](#0-1) 

Similarly, `PostedVaaV1Info` stores the `signature_set` pubkey but **no payer/owner field**: [3](#0-2) 

The `signature_set` is funded by the `payer` in `VerifySignatures` via `init_if_needed, payer = payer`: [4](#0-3) 

Because neither the `SignatureSet` account nor the `PostedVaaV1` account records who the original payer was, there is no on-chain data available to enforce a payer-match constraint in `CloseSignatureSet`.

### Impact Explanation

An attacker can:
1. Observe any live `signature_set` account on-chain (created by payer A via `verify_signatures`).
2. Derive or look up the associated `posted_vaa` PDA (which stores `has_one = signature_set`).
3. Call `close_signature_set` with `sol_destination = attacker_key` (attacker signs the tx), supplying the valid `posted_vaa` and `signature_set`.
4. Anchor's `close = sol_destination` macro transfers all lamports from `signature_set` to the attacker.

Payer A loses their rent deposit. The attacker gains it. This is direct theft of SOL from any user who has an open `signature_set` account.

### Likelihood Explanation

- All inputs (`posted_vaa`, `signature_set`) are public on-chain state — no secret knowledge required.
- The attacker only needs to sign a transaction with their own key.
- No privileged role, leaked key, or governance majority is needed.
- The attack is repeatable across every existing `signature_set` account.

### Recommendation

Record the original payer's pubkey inside `SignatureSet` at creation time (in `verify_signatures`), then add a constraint in `CloseSignatureSet` requiring `sol_destination.key() == signature_set.payer`. Alternatively, add a `constraint = sol_destination.key() == posted_vaa.payer` if the payer is stored in `PostedVaaV1Info`.

### Proof of Concept

```
1. Payer A calls verify_signatures → signature_set account S is created, funded with ~0.002 SOL by A.
2. Payer A (or anyone) calls post_vaa → posted_vaa PDA P is created with P.signature_set = S.
3. Attacker B constructs a transaction:
     close_signature_set(
       sol_destination = B,   // B signs
       posted_vaa      = P,   // valid PDA, has_one = S ✓
       signature_set   = S,   // valid account ✓
     )
4. Anchor closes S, transfers all lamports to B.
5. Assert: B.lamports increased by rent(S); A.lamports unchanged (never refunded).
``` [5](#0-4)

### Citations

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_signature_set.rs (L8-31)
```rust
pub struct CloseSignatureSet<'info> {
    #[account(mut)]
    sol_destination: Signer<'info>,

    /// Posted VAA.
    #[account(
        seeds = [
            PostedVaaV1::SEED_PREFIX,
            posted_vaa.message_hash().as_ref()
        ],
        bump,
        has_one = signature_set
    )]
    posted_vaa: Account<'info, LegacyAnchorized<PostedVaaV1>>,

    /// Signature set that may have been used to create the posted VAA account. If the `post_vaa_v1`
    /// instruction were used to create the posted VAA account, then the encoded signature set
    /// pubkey would be all zeroes.
    #[account(
        mut,
        close = sol_destination
    )]
    signature_set: Account<'info, AccountVariant<SignatureSet>>,
}
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/state/signature_set.rs (L17-26)
```rust
pub struct SignatureSet {
    /// Signatures of validators
    pub sig_verify_successes: Vec<bool>,

    /// Hash of the VAA message body.
    pub message_hash: MessageHash,

    /// Index of the guardian set
    pub guardian_set_index: u32,
}
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/state/posted_vaa_v1.rs (L13-44)
```rust
pub struct PostedVaaV1Info {
    /// Level of consistency requested by the emitter.
    pub consistency_level: u8,

    /// Time the message was submitted.
    pub timestamp: Timestamp,

    /// Pubkey of [SignatureSet](crate::state::SignatureSet) account that represents this VAA's
    /// signature verification.
    pub signature_set: Pubkey,

    /// Guardian set index used to verify signatures for [SignatureSet](crate::state::SignatureSet).
    ///
    /// NOTE: In the previous implementation, this member was referred to as the "posted timestamp",
    /// which is zero for VAA data (posted messages and VAAs resemble the same account schema). By
    /// changing this to the guardian set index, we patch a bug with verifying governance VAAs for
    /// the Core Bridge (other Core Bridge implementations require that the guardian set that
    /// attested for the governance VAA is the current one).
    pub guardian_set_index: u32,

    /// Unique ID for this message.
    pub nonce: u32,

    /// Sequence number of this message.
    pub sequence: u64,

    /// The Wormhole chain ID denoting the origin of this message.
    pub emitter_chain: u16,

    /// Emitter of the message.
    pub emitter_address: [u8; 32],
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
