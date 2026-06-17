### Title
Missing Authority Check in `close_signature_set` Allows Any Signer to Steal Relayer Rent Deposits — (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_signature_set.rs`)

---

### Summary

The `close_signature_set` instruction closes a `SignatureSet` account and transfers its lamports to `sol_destination`. The only constraint on `sol_destination` is that it must be a `Signer`. There is no check that `sol_destination` is the original payer/creator of the `signature_set`. Because `SignatureSet` stores no `payer` or `authority` field, any unprivileged attacker can supply their own pubkey as `sol_destination`, pass all Anchor constraints, and receive the full rent deposit that was paid by the original relayer.

---

### Finding Description

The `CloseSignatureSet` account context is defined as: [1](#0-0) 

The three constraints are:
1. `sol_destination` — must be a `Signer` (any signer qualifies).
2. `posted_vaa` — must be a valid PDA, and `posted_vaa.signature_set == signature_set.key()` (`has_one`).
3. `signature_set` — `close = sol_destination` transfers all lamports to `sol_destination`.

The `SignatureSet` struct stores only `sig_verify_successes`, `message_hash`, and `guardian_set_index`: [2](#0-1) 

There is no `payer` or `authority` field. The `has_one = signature_set` constraint only verifies that the `posted_vaa` references the provided `signature_set` account — it says nothing about who funded it. [3](#0-2) 

The `signature_set` is created by the legacy `verify_signatures` instruction, where the `payer` funds the account: [4](#0-3) 

That `payer` identity is never stored in the `SignatureSet` account, so it cannot be enforced at close time.

---

### Impact Explanation

An attacker can enumerate all `PostedVaaV1` accounts on-chain, identify those whose `signature_set` field is non-zero (i.e., created via the legacy `verify_signatures` + `post_vaa` path), and call `close_signature_set` with `sol_destination = attacker_pubkey` for each one. The full rent deposit of every such `SignatureSet` account is transferred to the attacker. The original relayer permanently loses their rent capital with no recourse. [5](#0-4) 

---

### Likelihood Explanation

- No privileged access is required; any funded keypair can execute this.
- All `PostedVaaV1` and `SignatureSet` accounts are public on-chain state, trivially enumerable.
- The attack is a single transaction per victim account.
- The `SignatureSet` account for 19 guardians is ~67 bytes, yielding ~0.00089 SOL per account. At scale (many relayers, many VAAs), the aggregate loss is material.

---

### Recommendation

Store the original `payer` pubkey inside the `SignatureSet` account at creation time (in `verify_signatures`), and add a `has_one = payer` or equivalent constraint in `CloseSignatureSet` so that only the original funder can designate the `sol_destination`. Alternatively, enforce `sol_destination` to equal a stored `payer` field via an explicit `constraint`.

---

### Proof of Concept

```
1. Keypair A (relayer): calls verify_signatures (pays rent for signature_set_A)
                        then calls post_vaa → posted_vaa_A.signature_set = signature_set_A.key()

2. Keypair B (attacker): constructs tx:
     close_signature_set {
         sol_destination: B,          // B signs
         posted_vaa:      posted_vaa_A,  // valid PDA, has_one passes
         signature_set:   signature_set_A // matches posted_vaa_A.signature_set
     }

3. Anchor checks:
   - B is Signer ✓
   - posted_vaa_A is valid PDA ✓
   - posted_vaa_A.signature_set == signature_set_A.key() ✓ (has_one)
   - close = sol_destination → all lamports transferred to B ✓

4. Result: B's balance increases by full rent of signature_set_A.
           A's rent deposit is permanently lost.
```

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
