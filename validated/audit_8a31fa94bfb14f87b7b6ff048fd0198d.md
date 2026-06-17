### Title
Rent Misallocation in `ReclaimRent`/`ReclaimTwapRent` — Rent Returned to `write_authority` Instead of Original `payer` — (File: `target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

---

### Summary

In `PostUpdate`, `PostUpdateAtomic`, and `PostTwapUpdate`, the `payer` account funds the creation of `price_update_account` / `twap_update_account`, while a separate `write_authority` is stored inside the account. When `reclaim_rent` / `reclaim_twap_rent` is called, the rent is returned to whoever holds the `write_authority` role — not the original `payer` who funded the account. If these two roles belong to different entities, the original payer permanently loses their rent to the write_authority.

---

### Finding Description

In `PostUpdate`, two distinct signers are accepted:

```rust
pub struct PostUpdate<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,                          // funds account creation
    ...
    #[account(init_if_needed, ..., payer = payer, space = PriceUpdateV2::LEN)]
    pub price_update_account: Account<'info, PriceUpdateV2>,
    ...
    pub write_authority: Signer<'info>,                // stored in account
}
``` [1](#0-0) 

Inside `post_price_update_from_vaa`, only `write_authority` is persisted into the account:

```rust
price_update_account.write_authority = write_authority.key();
``` [2](#0-1) 

The `PriceUpdateV2` struct stores only `write_authority` — the original `payer` is never recorded:

```rust
pub struct PriceUpdateV2 {
    pub write_authority: Pubkey,
    ...
}
``` [3](#0-2) 

When `reclaim_rent` is called, the constraint forces the signer to be the `write_authority`, and `close = payer` sends the rent to that same signer:

```rust
pub struct ReclaimRent<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(mut, close = payer, constraint = price_update_account.write_authority == payer.key() @ ReceiverError::WrongWriteAuthority)]
    pub price_update_account: Account<'info, PriceUpdateV2>,
}
``` [4](#0-3) 

The same pattern applies to `TwapUpdate` / `ReclaimTwapRent`: [5](#0-4) 

The original `payer` who funded the account has no mechanism to reclaim their rent. The `write_authority` receives lamports they did not pay for.

---

### Impact Explanation

When `payer != write_authority` in `PostUpdate` / `PostTwapUpdate`, the original `payer` permanently loses the rent they deposited for account creation. The `write_authority` can call `reclaim_rent` at any time and receive those lamports. This is a direct, unrecoverable financial loss for the payer — analogous to the reference bug where rent for closing a `RedeemRequest` was sent to the wrong party.

---

### Likelihood Explanation

The protocol explicitly supports `payer != write_authority` — both are independent `Signer` accounts in `PostUpdate` and `PostTwapUpdate`. Relayer services, integrators, or MEV bots commonly pay for account creation on behalf of users while designating a user-controlled key as `write_authority`. In such deployments, the user (write_authority) can immediately drain the relayer's deposited rent by calling `reclaim_rent`. No privileged access is required — any unprivileged transaction sender can trigger this by calling `post_update` with split roles and then `reclaim_rent` as the write_authority.

---

### Recommendation

Store the original `payer` pubkey inside `PriceUpdateV2` and `TwapUpdate` at account creation time. In `ReclaimRent` / `ReclaimTwapRent`, direct `close =` to the stored original payer, not to the `write_authority`. Alternatively, enforce `payer == write_authority` at `PostUpdate` time if the intent is that only one party funds and controls the account.

---

### Proof of Concept

1. Alice (relayer) calls `post_update` with `payer = Alice` and `write_authority = Bob`.
2. Alice's lamports fund the `price_update_account` creation (rent deposit).
3. `price_update_account.write_authority` is set to `Bob`; Alice's address is not stored anywhere.
4. Bob calls `reclaim_rent` with himself as `payer` — satisfying `price_update_account.write_authority == payer.key()`.
5. Anchor's `close = payer` transfers the full account rent to Bob.
6. Alice has permanently lost her rent deposit with no recourse.

The same four-step path applies identically to `post_twap_update` / `reclaim_twap_rent` via `TwapUpdate`.

### Citations

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L330-347)
```rust
pub struct PostUpdate<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(owner = config.wormhole @ ReceiverError::WrongVaaOwner)]
    /// CHECK: We aren't deserializing the VAA here but later with VaaAccount::load, which is the recommended way
    pub encoded_vaa: UncheckedAccount<'info>,
    #[account(seeds = [CONFIG_SEED.as_ref()], bump)]
    pub config: Account<'info, Config>,
    /// CHECK: This is just a PDA controlled by the program. There is currently no way to withdraw funds from it.
    #[account(mut, seeds = [TREASURY_SEED.as_ref(), &[params.treasury_id]], bump)]
    pub treasury: UncheckedAccount<'info>,
    /// The constraint is such that either the price_update_account is uninitialized or the write_authority is the write_authority.
    /// Pubkey::default() is the SystemProgram on Solana and it can't sign so it's impossible that price_update_account.write_authority == Pubkey::default() once the account is initialized
    #[account(init_if_needed, constraint = price_update_account.write_authority == Pubkey::default() || price_update_account.write_authority == write_authority.key() @ ReceiverError::WrongWriteAuthority , payer =payer, space = PriceUpdateV2::LEN)]
    pub price_update_account: Account<'info, PriceUpdateV2>,
    pub system_program: Program<'info, System>,
    pub write_authority: Signer<'info>,
}
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L396-402)
```rust
#[derive(Accounts)]
pub struct ReclaimRent<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(mut, close = payer, constraint = price_update_account.write_authority == payer.key() @ ReceiverError::WrongWriteAuthority)]
    pub price_update_account: Account<'info, PriceUpdateV2>,
}
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L404-410)
```rust
#[derive(Accounts)]
pub struct ReclaimTwapRent<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(mut, close = payer, constraint = twap_update_account.write_authority == payer.key() @ ReceiverError::WrongWriteAuthority)]
    pub twap_update_account: Account<'info, TwapUpdate>,
}
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L464-464)
```rust
            price_update_account.write_authority = write_authority.key();
```

**File:** target_chains/solana/pyth_solana_receiver_sdk/src/price_update.rs (L51-56)
```rust
pub struct PriceUpdateV2 {
    pub write_authority: Pubkey,
    pub verification_level: VerificationLevel,
    pub price_message: PriceFeedMessage,
    pub posted_slot: u64,
}
```
