### Title
Collected SOL Fees Permanently Locked in Treasury PDAs — No Withdrawal Mechanism - (File: `target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

### Summary

The `pyth-solana-receiver` Solana program collects SOL fees from every price update caller into treasury PDA accounts. The program itself explicitly acknowledges in code comments that there is no way to withdraw these funds. No governance instruction exists to transfer the accumulated SOL to any recipient, making all collected protocol fees permanently irrecoverable.

---

### Finding Description

Every call to `post_update`, `post_update_atomic`, or `post_twap_update` invokes `pay_single_update_fee`, which transfers `config.single_update_fee_in_lamports` SOL from the caller's wallet to a treasury PDA derived from `[TREASURY_SEED, treasury_id]`. [1](#0-0) 

The treasury PDA is a program-controlled account with no data. Up to 256 such accounts exist (one per `treasury_id` byte, used for write-lock load-balancing). [2](#0-1) [3](#0-2) [4](#0-3) 

The program's full instruction set consists of: `initialize`, `request_governance_authority_transfer`, `cancel_governance_authority_transfer`, `accept_governance_authority_transfer`, `set_data_sources`, `set_fee`, `set_wormhole_address`, `set_minimum_signatures`, `post_update`, `post_update_atomic`, `post_twap_update`, `reclaim_rent`, `reclaim_twap_rent`. None of these instructions transfer SOL out of any treasury PDA. The `reclaim_rent` and `reclaim_twap_rent` instructions only close user-owned `PriceUpdateV2` / `TwapUpdate` accounts back to the original payer — they do not touch the treasury. [5](#0-4) [6](#0-5) 

The governance authority can call `set_fee` to change the fee going forward, but cannot recover any SOL already accumulated. [7](#0-6) 

---

### Impact Explanation

All SOL paid as update fees by any caller accumulates permanently in the treasury PDAs and is irrecoverable by the Pyth governance authority or any other party. Since the treasury PDAs are program-derived accounts owned by the `pyth-solana-receiver` program, no external actor (including the governance authority) can sign for them outside of a program instruction. Because no such instruction exists, the funds are effectively burned from the perspective of the protocol. At any non-zero fee setting, every price update call permanently destroys protocol revenue.

---

### Likelihood Explanation

The entry path requires no privilege: any transaction sender can call `post_update`, `post_update_atomic`, or `post_twap_update`. These are the core, high-frequency instructions of the receiver program — they are called by relayers and integrators on every price feed update. The fee is configurable by governance (`set_fee`); if set to a non-zero value, every single update call contributes to the locked balance. The code comment itself confirms the design gap is known and unresolved.

---

### Recommendation

Add a governance-gated `withdraw_treasury` instruction that:
1. Requires the caller to be the `governance_authority` stored in the `Config` account.
2. Accepts a `treasury_id: u8`, a `recipient: Pubkey`, and an `amount: u64`.
3. Derives the treasury PDA with `[TREASURY_SEED, &[treasury_id]]` and uses `invoke_signed` with the PDA bump to transfer the requested lamports to the recipient.

This mirrors the pattern used by the Wormhole core-bridge `transfer_fees` governance instruction on Solana. [8](#0-7) 

---

### Proof of Concept

1. Governance sets a non-zero fee: calls `set_fee` with `single_update_fee_in_lamports = 1_000_000` (0.001 SOL).
2. Any relayer calls `post_update_atomic` with `treasury_id = 0`. `pay_single_update_fee` executes `system_instruction::transfer(payer, treasury_pda, 1_000_000)`.
3. After N updates, the treasury PDA at `[TREASURY_SEED, &[0]]` holds `N * 1_000_000` lamports.
4. Governance authority attempts to recover funds — no instruction exists in the program to do so.
5. Funds remain locked indefinitely. The governance authority can call `set_fee(0)` to stop further accumulation but cannot recover already-locked SOL. [9](#0-8) [10](#0-9)

### Citations

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L93-96)
```rust
    pub fn set_fee(ctx: Context<Governance>, single_update_fee_in_lamports: u64) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.single_update_fee_in_lamports = single_update_fee_in_lamports;
        Ok(())
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L288-293)
```rust
    pub fn reclaim_rent(_ctx: Context<ReclaimRent>) -> Result<()> {
        Ok(())
    }
    pub fn reclaim_twap_rent(_ctx: Context<ReclaimTwapRent>) -> Result<()> {
        Ok(())
    }
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L338-340)
```rust
    /// CHECK: This is just a PDA controlled by the program. There is currently no way to withdraw funds from it.
    #[account(mut, seeds = [TREASURY_SEED.as_ref(), &[params.treasury_id]], bump)]
    pub treasury: UncheckedAccount<'info>,
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L362-364)
```rust
    /// CHECK: This is just a PDA controlled by the program. There is currently no way to withdraw funds from it.
    #[account(mut, seeds = [TREASURY_SEED.as_ref(), &[params.treasury_id]], bump)]
    pub treasury: UncheckedAccount<'info>,
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L385-387)
```rust
    #[account(mut, seeds = [TREASURY_SEED.as_ref(), &[params.treasury_id]], bump)]
    /// CHECK: This is just a PDA controlled by the program. There is currently no way to withdraw funds from it.
    pub treasury: UncheckedAccount<'info>,
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L396-410)
```rust
#[derive(Accounts)]
pub struct ReclaimRent<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(mut, close = payer, constraint = price_update_account.write_authority == payer.key() @ ReceiverError::WrongWriteAuthority)]
    pub price_update_account: Account<'info, PriceUpdateV2>,
}

#[derive(Accounts)]
pub struct ReclaimTwapRent<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(mut, close = payer, constraint = twap_update_account.write_authority == payer.key() @ ReceiverError::WrongWriteAuthority)]
    pub twap_update_account: Account<'info, TwapUpdate>,
}
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L598-628)
```rust
fn pay_single_update_fee<'info>(
    config: &Account<'info, Config>,
    treasury: &AccountInfo<'info>,
    payer: &Signer<'info>,
) -> Result<()> {
    // Handle treasury payment
    let amount_to_pay = if treasury.lamports() == 0 && config.single_update_fee_in_lamports > 0 {
        Rent::get()?
            .minimum_balance(0)
            .max(config.single_update_fee_in_lamports)
    } else {
        config.single_update_fee_in_lamports
    };

    if payer.lamports()
        < Rent::get()?
            .minimum_balance(payer.data_len())
            .saturating_add(amount_to_pay)
    {
        return err!(ReceiverError::InsufficientFunds);
    }

    if amount_to_pay > 0 {
        let transfer_instruction =
            system_instruction::transfer(payer.key, treasury.key, amount_to_pay);
        anchor_lang::solana_program::program::invoke(
            &transfer_instruction,
            &[payer.to_account_info(), treasury.to_account_info()],
        )?;
    }
    Ok(())
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/governance/transfer_fees.rs (L134-176)
```rust
#[access_control(TransferFees::constraints(&ctx))]
fn transfer_fees(ctx: Context<TransferFees>, _args: EmptyArgs) -> Result<()> {
    let vaa = VaaAccount::load(&ctx.accounts.vaa).unwrap();

    // Create the claim account to provide replay protection. Because this instruction creates this
    // account every time it is executed, this account cannot be created again with this emitter
    // address, chain and sequence combination.
    utils::vaa::claim_vaa(
        CpiContext::new(
            ctx.accounts.system_program.key(),
            utils::vaa::ClaimVaa {
                claim: ctx.accounts.claim.to_account_info(),
                payer: ctx.accounts.payer.to_account_info(),
            },
        ),
        &crate::ID,
        &vaa,
        None,
    )?;

    let gov_payload = CoreBridgeGovPayload::try_from(vaa.try_payload().unwrap())
        .unwrap()
        .decree();
    let decree = gov_payload.transfer_fees().unwrap();

    let fee_collector = AsRef::<AccountInfo>::as_ref(&ctx.accounts.fee_collector);

    // Finally transfer collected fees to recipient.
    system_program::transfer(
        CpiContext::new_with_signer(
            ctx.accounts.system_program.key(),
            Transfer {
                from: fee_collector.to_account_info(),
                to: ctx.accounts.recipient.to_account_info(),
            },
            &[&[FEE_COLLECTOR_SEED_PREFIX, &[ctx.bumps.fee_collector]]],
        ),
        to_u64_unchecked(&U256::from_be_bytes(decree.amount())),
    )?;

    // Done.
    Ok(())
}
```

**File:** target_chains/solana/sdk/js/pyth_solana_receiver/src/address.ts (L56-71)
```typescript
export function getRandomTreasuryId() {
  return Math.floor(Math.random() * 256);
}

/**
 * Returns the address of a treasury account from the Pyth Solana Receiver program.
 */
export const getTreasuryPda = (
  treasuryId: number,
  receiverProgramId: PublicKey,
) => {
  return PublicKey.findProgramAddressSync(
    [IsomorphicBuffer.from("treasury"), IsomorphicBuffer.from([treasuryId])],
    receiverProgramId,
  )[0];
};
```
