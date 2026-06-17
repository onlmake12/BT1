### Title
Protocol Fee Revenue Permanently Locked in Treasury PDAs With No Withdrawal Mechanism - (File: `target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

### Summary
The `pyth-solana-receiver` Solana program collects SOL fees from every price update submission into treasury PDAs, but the program contains no instruction to withdraw or redistribute those accumulated lamports. The code itself acknowledges this with an explicit comment. All fee revenue is permanently locked.

### Finding Description
Every call to `post_update`, `post_update_atomic`, or `post_twap_update` invokes `pay_single_update_fee`, which performs a system-program transfer of `single_update_fee_in_lamports` (plus rent-minimum on first use) from the caller's wallet into a treasury PDA derived from `[TREASURY_SEED, treasury_id]`. [1](#0-0) 

There are 256 possible treasury PDAs (one per `u8` value of `treasury_id`), each accumulating lamports independently. [2](#0-1) 

The program's own comments in all three account structs that reference the treasury explicitly state the problem:

```
/// CHECK: This is just a PDA controlled by the program.
/// There is currently no way to withdraw funds from it.
``` [3](#0-2) [4](#0-3) [5](#0-4) 

Reviewing the complete set of program instructions in `#[program]`, there is no `withdraw_fees`, `drain_treasury`, or equivalent instruction. The governance instructions (`set_fee`, `set_data_sources`, `set_wormhole_address`, `set_minimum_signatures`) only modify the `Config` account and have no access to treasury PDAs. `reclaim_rent` and `reclaim_twap_rent` only close `PriceUpdateV2` / `TwapUpdate` accounts back to the original payer — not the treasury. [6](#0-5) 

Because the treasury is a PDA owned by the program, only the program itself can authorize a transfer out. With no such instruction existing, the lamports are irrecoverable.

### Impact Explanation
All SOL paid as update fees by every caller of `post_update`, `post_update_atomic`, and `post_twap_update` accumulates in the treasury PDAs and can never be retrieved by the protocol or redistributed to any party. This is a permanent, compounding loss of protocol revenue proportional to total update volume. The `single_update_fee_in_lamports` is a governance-configurable value, meaning the protocol actively intends to collect fees — yet the collection mechanism has no corresponding disbursement path. [7](#0-6) 

### Likelihood Explanation
Certainty — this occurs on every single price update submitted to the Solana receiver. No special conditions, attacker, or race are required. Any unprivileged transaction sender calling `post_update` or `post_update_atomic` (the standard integration path for all Pyth consumers on Solana) triggers the fee transfer. The protocol is live and these calls happen continuously.

### Recommendation
Add a governance-gated instruction (callable only by `governance_authority`) that transfers a specified amount of lamports from a given treasury PDA to a recipient address, using a direct lamport manipulation or a CPI to the system program signed by the PDA. Example skeleton:

```rust
pub fn withdraw_treasury_fees(
    ctx: Context<WithdrawTreasuryFees>,
    treasury_id: u8,
    amount: u64,
    recipient: Pubkey,
) -> Result<()> {
    **ctx.accounts.treasury.lamports.borrow_mut() -= amount;
    **ctx.accounts.recipient.lamports.borrow_mut() += amount;
    Ok(())
}
```

The `WithdrawTreasuryFees` context should constrain the signer to `config.governance_authority` and derive the treasury PDA with the same seeds used during fee collection.

### Proof of Concept
1. Deploy `pyth-solana-receiver` with `single_update_fee_in_lamports = 1_000_000` (0.001 SOL).
2. Call `post_update_atomic` with a valid VAA and Merkle proof. Observe that `treasury_id=0` PDA balance increases by `max(rent_minimum, 1_000_000)` lamports.
3. Repeat 1000 times. Treasury PDA holds ~1 SOL.
4. Attempt to call any program instruction to recover those lamports — no such instruction exists. The funds are confirmed permanently locked. [8](#0-7)

### Citations

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L44-293)
```rust
#[program]
pub mod pyth_solana_receiver {
    use super::*;

    pub fn initialize(ctx: Context<Initialize>, initial_config: Config) -> Result<()> {
        require!(
            initial_config.minimum_signatures > 0,
            ReceiverError::ZeroMinimumSignatures
        );
        let config = &mut ctx.accounts.config;
        **config = initial_config;
        Ok(())
    }

    pub fn request_governance_authority_transfer(
        ctx: Context<Governance>,
        target_governance_authority: Pubkey,
    ) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.target_governance_authority = Some(target_governance_authority);
        Ok(())
    }

    pub fn cancel_governance_authority_transfer(ctx: Context<Governance>) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.target_governance_authority = None;
        Ok(())
    }

    pub fn accept_governance_authority_transfer(
        ctx: Context<AcceptGovernanceAuthorityTransfer>,
    ) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.governance_authority = config.target_governance_authority.ok_or(error!(
            ReceiverError::NonexistentGovernanceAuthorityTransferRequest
        ))?;
        config.target_governance_authority = None;
        Ok(())
    }

    pub fn set_data_sources(
        ctx: Context<Governance>,
        valid_data_sources: Vec<DataSource>,
    ) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.valid_data_sources = valid_data_sources;
        Ok(())
    }

    pub fn set_fee(ctx: Context<Governance>, single_update_fee_in_lamports: u64) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.single_update_fee_in_lamports = single_update_fee_in_lamports;
        Ok(())
    }

    pub fn set_wormhole_address(ctx: Context<Governance>, wormhole: Pubkey) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.wormhole = wormhole;
        Ok(())
    }

    pub fn set_minimum_signatures(ctx: Context<Governance>, minimum_signatures: u8) -> Result<()> {
        let config = &mut ctx.accounts.config;
        require!(minimum_signatures > 0, ReceiverError::ZeroMinimumSignatures);
        config.minimum_signatures = minimum_signatures;
        Ok(())
    }

    /// Post a price update using a VAA and a MerklePriceUpdate.
    /// This function allows you to post a price update in a single transaction.
    /// Compared to `post_update`, it only checks whatever signatures are present in the provided VAA and doesn't fail if the number of signatures is lower than the Wormhole quorum of two thirds of the guardians.
    /// The number of signatures that were in the VAA is stored in the `VerificationLevel` of the `PriceUpdateV2` account.
    ///
    /// We recommend using `post_update_atomic` with 5 signatures. This is close to the maximum signatures you can verify in one transaction without exceeding the transaction size limit.
    ///
    /// # Warning
    ///
    /// Using partially verified price updates is dangerous, as it lowers the threshold of guardians that need to collude to produce a malicious price update.
    pub fn post_update_atomic(
        ctx: Context<PostUpdateAtomic>,
        params: PostUpdateAtomicParams,
    ) -> Result<()> {
        let config = &ctx.accounts.config;
        let guardian_set =
            deserialize_guardian_set_checked(&ctx.accounts.guardian_set, &config.wormhole)?;

        // This section is borrowed from https://github.com/wormhole-foundation/wormhole/blob/wen/solana-rewrite/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/verify_encoded_vaa_v1.rs#L59
        let vaa = Vaa::parse(&params.vaa).map_err(|_| ReceiverError::DeserializeVaaFailed)?;
        // Must be V1.
        require_eq!(vaa.version(), 1, ReceiverError::InvalidVaaVersion);

        // Make sure the encoded guardian set index agrees with the guardian set account's index.
        let guardian_set = guardian_set.inner();
        require_eq!(
            vaa.guardian_set_index(),
            guardian_set.index,
            ReceiverError::GuardianSetMismatch
        );

        let guardian_keys = &guardian_set.keys;
        let quorum = quorum(guardian_keys.len());
        require_gte!(
            vaa.signature_count(),
            config.minimum_signatures,
            ReceiverError::InsufficientGuardianSignatures
        );
        let verification_level = if usize::from(vaa.signature_count()) >= quorum {
            VerificationLevel::Full
        } else {
            VerificationLevel::Partial {
                num_signatures: vaa.signature_count(),
            }
        };

        // Generate the same message hash (using keccak) that the Guardians used to generate their
        // signatures. This message hash will be hashed again to produce the digest for
        // `secp256k1_recover`.
        let digest = keccak::hash(keccak::hash(vaa.body().as_ref()).as_ref());

        let mut last_guardian_index = None;
        for sig in vaa.signatures() {
            // We do not allow for non-increasing guardian signature indices.
            let index = usize::from(sig.guardian_index());
            if let Some(last_index) = last_guardian_index {
                require!(index > last_index, ReceiverError::InvalidGuardianOrder);
            }

            // Does this guardian index exist in this guardian set?
            let guardian_pubkey = guardian_keys
                .get(index)
                .ok_or_else(|| error!(ReceiverError::InvalidGuardianIndex))?;

            // Now verify that the signature agrees with the expected Guardian's pubkey.
            verify_guardian_signature(&sig, guardian_pubkey, digest.as_ref())?;

            last_guardian_index = Some(index);
        }
        // End borrowed section

        let payer = &ctx.accounts.payer;
        let write_authority: &Signer<'_> = &ctx.accounts.write_authority;
        let treasury = &ctx.accounts.treasury;
        let price_update_account = &mut ctx.accounts.price_update_account;

        let vaa_components = VaaComponents {
            verification_level,
            emitter_address: vaa.body().emitter_address(),
            emitter_chain: vaa.body().emitter_chain(),
        };

        post_price_update_from_vaa(
            config,
            payer,
            write_authority,
            treasury,
            price_update_account,
            &vaa_components,
            vaa.payload().as_ref(),
            &params.merkle_price_update,
        )?;

        Ok(())
    }

    /// Post a price update using an encoded_vaa account and a MerklePriceUpdate calldata.
    /// This should be called after the client has already verified the Vaa via the Wormhole contract.
    /// Check out target_chains/solana/cli/src/main.rs for an example of how to do this.
    pub fn post_update(ctx: Context<PostUpdate>, params: PostUpdateParams) -> Result<()> {
        let config = &ctx.accounts.config;
        let payer: &Signer<'_> = &ctx.accounts.payer;
        let write_authority: &Signer<'_> = &ctx.accounts.write_authority;
        let encoded_vaa = VaaAccount::load(&ctx.accounts.encoded_vaa)?; // IMPORTANT: This line checks that the encoded_vaa has ProcessingStatus::Verified. This check is critical otherwise the program could be tricked into accepting unverified VAAs.
        let treasury: &AccountInfo<'_> = &ctx.accounts.treasury;
        let price_update_account: &mut Account<'_, PriceUpdateV2> =
            &mut ctx.accounts.price_update_account;

        let vaa_components = VaaComponents {
            verification_level: VerificationLevel::Full,
            emitter_address: encoded_vaa.try_emitter_address()?,
            emitter_chain: encoded_vaa.try_emitter_chain()?,
        };

        post_price_update_from_vaa(
            config,
            payer,
            write_authority,
            treasury,
            price_update_account,
            &vaa_components,
            encoded_vaa.try_payload()?.as_ref(),
            &params.merkle_price_update,
        )?;

        Ok(())
    }

    /// Post a TWAP (time weighted average price) update for a given time window.
    /// This should be called after the client has already verified the VAAs via the Wormhole contract.
    /// Check out target_chains/solana/cli/src/main.rs for an example of how to do this.
    pub fn post_twap_update(
        ctx: Context<PostTwapUpdate>,
        params: PostTwapUpdateParams,
    ) -> Result<()> {
        let config = &ctx.accounts.config;
        let payer: &Signer<'_> = &ctx.accounts.payer;
        let write_authority: &Signer<'_> = &ctx.accounts.write_authority;

        // IMPORTANT: These lines check that the encoded VAAs have ProcessingStatus::Verified.
        // These checks are critical otherwise the program could be tricked into accepting unverified VAAs.
        let start_encoded_vaa = VaaAccount::load(&ctx.accounts.start_encoded_vaa)?;
        let end_encoded_vaa = VaaAccount::load(&ctx.accounts.end_encoded_vaa)?;

        let treasury: &AccountInfo<'_> = &ctx.accounts.treasury;
        let twap_update_account: &mut Account<'_, TwapUpdate> =
            &mut ctx.accounts.twap_update_account;

        let start_vaa_components = VaaComponents {
            verification_level: VerificationLevel::Full,
            emitter_address: start_encoded_vaa.try_emitter_address()?,
            emitter_chain: start_encoded_vaa.try_emitter_chain()?,
        };
        let end_vaa_components = VaaComponents {
            verification_level: VerificationLevel::Full,
            emitter_address: end_encoded_vaa.try_emitter_address()?,
            emitter_chain: end_encoded_vaa.try_emitter_chain()?,
        };

        post_twap_update_from_vaas(
            config,
            payer,
            write_authority,
            treasury,
            twap_update_account,
            &start_vaa_components,
            &end_vaa_components,
            start_encoded_vaa.try_payload()?.as_ref(),
            end_encoded_vaa.try_payload()?.as_ref(),
            &params.start_merkle_price_update,
            &params.end_merkle_price_update,
        )?;

        Ok(())
    }

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

**File:** target_chains/solana/pyth_solana_receiver_sdk/src/pda.rs (L6-10)
```rust
// There is one treasury for each u8 value
// This is to load balance the write load
pub fn get_treasury_address(treasury_id: u8) -> Pubkey {
    Pubkey::find_program_address(&[TREASURY_SEED.as_ref(), &[treasury_id]], &ID).0
}
```

**File:** target_chains/solana/pyth_solana_receiver_sdk/src/config.rs (L10-10)
```rust
    pub single_update_fee_in_lamports: u64,  // The fee in lamports for a single price update
```
