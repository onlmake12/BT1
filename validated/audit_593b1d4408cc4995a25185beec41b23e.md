### Title
Missing Validation of Critical Config Fields in `initialize` Enables Permanent DoS of All Price Update Instructions — (File: `target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

---

### Summary

The `initialize` instruction of the `pyth-solana-receiver` program accepts a caller-supplied `Config` struct and writes it to the config PDA with only one validation (`minimum_signatures > 0`). Critical fields — `valid_data_sources`, `wormhole`, and `governance_authority` — are not validated. Because the `Initialize` account context imposes no access control on the `payer` signer, any unprivileged transaction sender can race the deployer and initialize the config with invalid values, causing permanent DoS of all price-update instructions with no governance recovery path.

---

### Finding Description

The `initialize` handler writes the caller-supplied `initial_config` directly to the config PDA:

```rust
pub fn initialize(ctx: Context<Initialize>, initial_config: Config) -> Result<()> {
    require!(
        initial_config.minimum_signatures > 0,
        ReceiverError::ZeroMinimumSignatures
    );
    let config = &mut ctx.accounts.config;
    **config = initial_config;
    Ok(())
}
``` [1](#0-0) 

The `Initialize` account context has no authority constraint on `payer`:

```rust
pub struct Initialize<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(init, space = Config::LEN, payer=payer, seeds = [CONFIG_SEED.as_ref()], bump)]
    pub config: Account<'info, Config>,
    pub system_program: Program<'info, System>,
}
``` [2](#0-1) 

The `Config` struct contains three fields that are never validated:

| Field | Missing check | Downstream consequence |
|---|---|---|
| `valid_data_sources: Vec<DataSource>` | Non-empty | Empty list → every `post_update` / `post_update_atomic` / `post_twap_update` call fails emitter validation |
| `wormhole: Pubkey` | Non-zero | Zero address → `encoded_vaa` owner check (`owner = config.wormhole`) always fails |
| `governance_authority: Pubkey` | Non-zero | Zero address → no signer can ever pass the `Governance` constraint, permanently locking all governance instructions | [3](#0-2) 

The `PostUpdate` and `PostUpdateAtomic` contexts enforce both the `wormhole` owner check and the data-source emitter check at account-constraint level: [4](#0-3) [5](#0-4) 

The governance recovery path (`set_data_sources`, `set_wormhole_address`, etc.) all require the `Governance` constraint, which checks `payer.key() == config.governance_authority`. If `governance_authority` is `Pubkey::default()`, no signer can satisfy this constraint, making the broken state irrecoverable. [6](#0-5) 

---

### Impact Explanation

An attacker who front-runs the deployer's `initialize` transaction can set:
- `valid_data_sources = []` — all price-update instructions revert permanently
- `wormhole = Pubkey::default()` — all VAA-based price-update instructions revert permanently
- `governance_authority = Pubkey::default()` — no governance instruction can ever execute

Because the config PDA is derived from a fixed seed and uses `init`, it can only be created once. The broken state is permanent and unrecoverable without a program upgrade. Every downstream consumer of Pyth price feeds on Solana is affected.

---

### Likelihood Explanation

The `initialize` instruction is called exactly once per deployment. During a new deployment or redeployment to a new program ID, there is a window between program deployment and config initialization. An attacker monitoring the mempool or the program's account state can submit a competing `initialize` transaction with a higher priority fee. No privileged access, leaked key, or social engineering is required — only the ability to submit a Solana transaction.

---

### Recommendation

1. **Add a deployer authority check**: Derive the config PDA from a known deployer key or require the `payer` to match a hardcoded upgrade authority, so only the legitimate deployer can call `initialize`.
2. **Validate `valid_data_sources` is non-empty** at initialization time.
3. **Validate `wormhole != Pubkey::default()`** at initialization time.
4. **Validate `governance_authority != Pubkey::default()`** at initialization time.

Example additions to the handler:

```rust
require!(!initial_config.valid_data_sources.is_empty(), ReceiverError::NoDataSources);
require!(initial_config.wormhole != Pubkey::default(), ReceiverError::InvalidWormholeAddress);
require!(initial_config.governance_authority != Pubkey::default(), ReceiverError::InvalidGovernanceAuthority);
```

---

### Proof of Concept

1. Deploy `pyth-solana-receiver` to a new program ID (or observe a pending deployment).
2. Before the deployer's `initialize` transaction lands, submit a competing transaction calling `initialize` with:
   - `governance_authority = Pubkey::default()`
   - `wormhole = Pubkey::default()`
   - `valid_data_sources = []`
   - `minimum_signatures = 1` (passes the only existing check)
3. Observe that the config PDA is now initialized with invalid values.
4. Attempt to call `post_update` or `post_update_atomic` — both revert because the `encoded_vaa` owner check (`owner = config.wormhole`) fails against `Pubkey::default()`.
5. Attempt to call `set_data_sources` or `set_wormhole_address` — both revert because no signer can match `governance_authority = Pubkey::default()`.
6. The program is permanently DoS'd with no on-chain recovery path.

### Citations

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L48-56)
```rust
    pub fn initialize(ctx: Context<Initialize>, initial_config: Config) -> Result<()> {
        require!(
            initial_config.minimum_signatures > 0,
            ReceiverError::ZeroMinimumSignatures
        );
        let config = &mut ctx.accounts.config;
        **config = initial_config;
        Ok(())
    }
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L296-304)
```rust
#[derive(Accounts)]
#[instruction(initial_config : Config)]
pub struct Initialize<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(init, space = Config::LEN, payer=payer, seeds = [CONFIG_SEED.as_ref()], bump)]
    pub config: Account<'info, Config>,
    pub system_program: Program<'info, System>,
}
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L306-315)
```rust
#[derive(Accounts)]
pub struct Governance<'info> {
    #[account(constraint =
        payer.key() == config.governance_authority @
        ReceiverError::GovernanceAuthorityMismatch
    )]
    pub payer: Signer<'info>,
    #[account(mut, seeds = [CONFIG_SEED.as_ref()], bump)]
    pub config: Account<'info, Config>,
}
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L333-347)
```rust
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

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L380-394)
```rust
    #[account(
        owner = config.wormhole @ ReceiverError::WrongGuardianSetOwner)]
    pub guardian_set: UncheckedAccount<'info>,
    #[account(seeds = [CONFIG_SEED.as_ref()], bump)]
    pub config: Account<'info, Config>,
    #[account(mut, seeds = [TREASURY_SEED.as_ref(), &[params.treasury_id]], bump)]
    /// CHECK: This is just a PDA controlled by the program. There is currently no way to withdraw funds from it.
    pub treasury: UncheckedAccount<'info>,
    /// The constraint is such that either the price_update_account is uninitialized or the write_authority is the write_authority.
    /// Pubkey::default() is the SystemProgram on Solana and it can't sign so it's impossible that price_update_account.write_authority == Pubkey::default() once the account is initialized
    #[account(init_if_needed, constraint = price_update_account.write_authority == Pubkey::default() || price_update_account.write_authority == write_authority.key() @ ReceiverError::WrongWriteAuthority, payer = payer, space = PriceUpdateV2::LEN)]
    pub price_update_account: Account<'info, PriceUpdateV2>,
    pub system_program: Program<'info, System>,
    pub write_authority: Signer<'info>,
}
```

**File:** target_chains/solana/pyth_solana_receiver_sdk/src/config.rs (L5-12)
```rust
pub struct Config {
    pub governance_authority: Pubkey, // This authority can update the other fields
    pub target_governance_authority: Option<Pubkey>, // This field is used for a two-step governance authority transfer
    pub wormhole: Pubkey,                            // The address of the wormhole receiver
    pub valid_data_sources: Vec<DataSource>, // The list of valid data sources for oracle price updates
    pub single_update_fee_in_lamports: u64,  // The fee in lamports for a single price update
    pub minimum_signatures: u8, // The minimum number of signatures required to accept a VAA
}
```
