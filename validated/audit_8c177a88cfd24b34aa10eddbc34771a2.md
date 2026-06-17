The code is clear and the analysis is straightforward. Let me confirm the exact constraints in the account struct.

The `CloseSignatureSet` account struct at [1](#0-0)  enforces:

1. `sol_destination` must be a **signer** — but there is **no constraint** tying it to the original payer/depositor.
2. `posted_vaa` must be a valid PDA with `has_one = signature_set`.
3. `signature_set` is closed to `sol_destination` via `close = sol_destination`.

The `PostedVaaV1` struct stores no `payer` or `write_authority` field — only `consistency_level`, `timestamp`, `signature_set` (pubkey), `guardian_set_index`, `nonce`, `sequence`, `emitter_chain`, `emitter_address`, and `payload`. [2](#0-1) 

The `SignatureSet` struct similarly stores no `payer` field — only `sig_verify_successes`, `message_hash`, and `guardian_set_index`. [3](#0-2) 

The `signature_set` is funded by the `payer` in `verify_signatures`, but that payer is never recorded anywhere. [4](#0-3) 

---

### Title
Unprivileged Lamport Theft via `close_signature_set` — Missing `sol_destination` Authority Check — (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_signature_set.rs`)

### Summary
`close_signature_set` allows any signer to supply themselves as `sol_destination` and drain the rent-exempt lamports from any `SignatureSet` account linked to any `PostedVaaV1`, regardless of who originally funded the `SignatureSet`.

### Finding Description
The `CloseSignatureSet` account context requires only that `sol_destination` is a signer and that `posted_vaa` has `has_one = signature_set`. There is no constraint that `sol_destination` equals the original payer of the `signature_set` account. Because neither `PostedVaaV1` nor `SignatureSet` records the original depositor, the program has no on-chain basis to enforce this invariant. The `close = sol_destination` Anchor macro unconditionally transfers all lamports from `signature_set` to whichever signer is passed as `sol_destination`. [5](#0-4) 

### Impact Explanation
An attacker can monitor the chain for any `PostedVaaV1` account whose `signature_set` field is non-zero (i.e., created via the legacy `verify_signatures` + `post_vaa` path), then call `close_signature_set` with their own keypair as `sol_destination`. The `signature_set` account is closed and its rent-exempt lamports are transferred to the attacker rather than the original payer. The original payer permanently loses those lamports.

### Likelihood Explanation
The attack requires no privilege, no leaked key, and no off-chain coordination. All inputs (`posted_vaa`, `signature_set`) are public on-chain accounts. The attacker simply needs to submit the transaction before the original payer reclaims their rent. This is trivially automatable by monitoring for `PostedVaaV1` accounts with non-zero `signature_set` pubkeys.

### Recommendation
Record the original payer of the `SignatureSet` account (or the `PostedVaaV1` account) at creation time, and add a constraint in `CloseSignatureSet` requiring `sol_destination` to match that stored authority. For example, add a `payer: Pubkey` field to `SignatureSet` during `verify_signatures`, then enforce `constraint = sol_destination.key() == signature_set.payer` in the close context.

### Proof of Concept
1. User A calls `verify_signatures` (funding a `signature_set` keypair) and then `post_vaa` to create a `posted_vaa` account. The `posted_vaa.signature_set` field now holds the pubkey of User A's `signature_set`.
2. Attacker B observes the `posted_vaa` account on-chain.
3. Attacker B calls `close_signature_set` passing:
   - `sol_destination` = Attacker B's keypair (signs the transaction)
   - `posted_vaa` = User A's `posted_vaa` account
   - `signature_set` = User A's `signature_set` account (matches `posted_vaa.signature_set` via `has_one`)
4. All constraints pass. Anchor's `close = sol_destination` transfers all lamports from `signature_set` to Attacker B.
5. User A's `signature_set` is closed; Attacker B receives the rent-exempt lamports. User A cannot reclaim them.

### Citations

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_signature_set.rs (L7-31)
```rust
#[derive(Accounts)]
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

**File:** target_chains/solana/programs/core-bridge/src/legacy/state/posted_vaa_v1.rs (L12-53)
```rust
#[derive(Debug, AnchorSerialize, AnchorDeserialize, Clone, PartialEq, Eq, InitSpace)]
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

/// Account used to store a verified VAA.
#[derive(Debug, AnchorSerialize, AnchorDeserialize, Clone)]
pub struct PostedVaaV1 {
    /// VAA metadata.
    pub info: PostedVaaV1Info,
    /// Message payload.
    pub payload: Vec<u8>,
}
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/state/signature_set.rs (L16-34)
```rust
#[account]
pub struct SignatureSet {
    /// Signatures of validators
    pub sig_verify_successes: Vec<bool>,

    /// Hash of the VAA message body.
    pub message_hash: MessageHash,

    /// Index of the guardian set
    pub guardian_set_index: u32,
}

impl LegacyAccount for SignatureSet {
    const LEGACY_DISCRIMINATOR: &'static [u8] = &[];

    fn program_id() -> Pubkey {
        crate::ID
    }
}
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/verify_signatures.rs (L36-57)
```rust
pub struct VerifySignatures<'info> {
    #[account(mut)]
    payer: Signer<'info>,

    /// Guardian set used for signature verification. These pubkeys were passed into the Sig Verify
    /// native program to do its signature verification.
    #[account(
        seeds = [
            GuardianSet::SEED_PREFIX,
            guardian_set.inner().index.to_be_bytes().as_ref()
        ],
        bump,
    )]
    guardian_set: Account<'info, AccountVariant<GuardianSet>>,

    /// Stores signature validation from Sig Verify native program.
    #[account(
        init_if_needed,
        payer = payer,
        space = SignatureSet::compute_size(guardian_set.inner().keys.len())
    )]
    signature_set: Account<'info, SignatureSet>,
```
