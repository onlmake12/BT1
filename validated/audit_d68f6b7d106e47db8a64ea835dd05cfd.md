The vulnerability is real and exploitable. Here is the full analysis:

---

### Title
Unprivileged Caller Can Steal Rent from Any Legacy `SignatureSet` Account via `close_signature_set` — (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_signature_set.rs`)

### Summary

`close_signature_set` enforces no ownership check on who receives the lamports. Any signer can drain the rent deposit from a `SignatureSet` account they did not fund, as long as they supply the matching `posted_vaa`.

### Finding Description

The `CloseSignatureSet` account context has three constraints:

1. `sol_destination` must be a `Signer` — **any** signer qualifies.
2. `posted_vaa` is validated as a PDA and must satisfy `has_one = signature_set` — this only checks that `posted_vaa.info.signature_set == signature_set.key()`.
3. `signature_set` is closed with `close = sol_destination` — all lamports go to whoever signed. [1](#0-0) 

The `SignatureSet` struct stores only `sig_verify_successes`, `message_hash`, and `guardian_set_index` — there is no `payer` or `authority` field recording who originally funded the account. [2](#0-1) 

During the legacy `post_vaa` flow, the `PostedVaaV1` account is written with `signature_set: signature_set.key()`, permanently linking the two accounts. [3](#0-2) 

Because `posted_vaa` is a PDA derived from the message hash and is publicly readable on-chain, any attacker can enumerate all `PostedVaaV1` accounts, find their `signature_set` pubkey, and call `close_signature_set` with themselves as `sol_destination`.

### Impact Explanation

Every relayer that used the legacy `verify_signatures` + `post_vaa` flow paid rent to create a `SignatureSet` account. That rent is permanently lost to any attacker who calls `close_signature_set` first. The `SignatureSet` account is a keypair account (not a PDA), so its address is unique per relayer invocation. The attacker receives the full lamport balance of the account.

### Likelihood Explanation

The attack requires no privileges, no leaked keys, and no governance access. All inputs (`posted_vaa` PDA, `signature_set` pubkey stored inside it) are publicly visible on-chain. The attacker only needs to sign a transaction with their own wallet. Any `SignatureSet` that has not yet been closed is vulnerable.

### Recommendation

Add an authority check. The simplest fix is to record the original payer inside `SignatureSet` at creation time and require `sol_destination.key() == signature_set.payer` in `CloseSignatureSet`. Alternatively, require `sol_destination` to be the same account that signed the original `verify_signatures` instruction by storing a `payer: Pubkey` field in `SignatureSet`.

### Proof of Concept

```
// Precondition: relayer_A ran verify_signatures + post_vaa for some VAA.
// posted_vaa_pda.info.signature_set == relayer_A_sig_set.key()

attacker_wallet.sign(close_signature_set {
    sol_destination: attacker_wallet,   // any signer — no ownership check
    posted_vaa:      posted_vaa_pda,    // publicly known PDA
    signature_set:   relayer_A_sig_set, // pubkey read from posted_vaa.info.signature_set
})
// Result: has_one passes, close = sol_destination drains relayer_A_sig_set lamports to attacker.
```

The invariant that only the original depositor of rent can recover it is broken because `CloseSignatureSet` never verifies that `sol_destination` is the account that originally funded `signature_set`. [4](#0-3)

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

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_vaa.rs (L157-164)
```rust
    let signature_set = &ctx.accounts.signature_set;
    ctx.accounts.posted_vaa.set_inner(
        PostedVaaV1 {
            info: PostedVaaV1Info {
                consistency_level,
                timestamp: timestamp.into(),
                signature_set: signature_set.key(),
                guardian_set_index: signature_set.guardian_set_index,
```
