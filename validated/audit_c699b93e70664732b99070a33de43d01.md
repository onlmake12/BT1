### Title
Insecure Initialization — Any Unprivileged Caller Can Front-Run Deployer and Set Arbitrary `governance_authority` and `wormhole` Address - (File: `target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

---

### Summary
The `initialize` instruction of the `pyth-solana-receiver` Anchor program has no access control. Any unprivileged transaction sender can call it before the legitimate deployer and supply an arbitrary `Config`, including a malicious `governance_authority`, a fake `wormhole` program address, attacker-controlled `valid_data_sources`, and a minimal `minimum_signatures` value of 1. Because the config PDA is a singleton (seeded by `CONFIG_SEED`), the first caller wins and the deployer cannot re-initialize without redeploying the program.

---

### Finding Description

The `initialize` function in `pyth-solana-receiver` writes the caller-supplied `initial_config` directly into the singleton config PDA with no restriction on who the caller is:

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

The `Initialize` accounts struct requires only that `payer` is a signer — any wallet qualifies:

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
``` [2](#0-1) 

There is no check against the program's upgrade authority, a known deployer key, or any other trusted address. The `Config` struct that gets written contains all security-critical parameters: [2](#0-1) 

```rust
pub struct Config {
    pub governance_authority: Pubkey,
    pub target_governance_authority: Option<Pubkey>,
    pub wormhole: Pubkey,
    pub valid_data_sources: Vec<DataSource>,
    pub single_update_fee_in_lamports: u64,
    pub minimum_signatures: u8,
}
``` [3](#0-2) [2](#0-1) 

All subsequent governance operations (`set_data_sources`, `set_fee`, `set_wormhole_address`, `set_minimum_signatures`, `request_governance_authority_transfer`) are gated behind `governance_authority`:

```rust
pub struct Governance<'info> {
    #[account(constraint =
        payer.key() == config.governance_authority @
        ReceiverError::GovernanceAuthorityMismatch
    )]
    pub payer: Signer<'info>,
    ...
}
``` [4](#0-3) 

An attacker who front-runs `initialize` owns `governance_authority` and can change every parameter at will.

The same pattern exists in `pyth-price-store`'s `initialize`, where any payer can set an arbitrary `authority` key that gates all `InitializePublisher` calls: [5](#0-4) 

---

### Impact Explanation

An attacker who wins the race to call `initialize` on `pyth-solana-receiver` can:

1. **Set themselves as `governance_authority`** — gaining permanent control over all governance operations (data sources, fees, wormhole address, signature threshold).
2. **Set `wormhole` to a malicious program** — bypassing guardian-set VAA verification entirely in `post_update_atomic`, allowing fake price data to be accepted.
3. **Set `valid_data_sources` to attacker-controlled emitters** — making the receiver accept price updates from arbitrary sources.
4. **Set `minimum_signatures = 1`** — reducing the guardian quorum to a single signature, trivially forgeable.

All downstream consumers of `PriceUpdateV2` accounts (DeFi protocols, liquidation bots, etc.) would receive attacker-manipulated prices. The deployer cannot recover without redeploying the program, since the config PDA is a singleton and Anchor's `init` constraint prevents re-initialization.

---

### Likelihood Explanation

The window between program deployment and the deployer's `initialize` call is observable on-chain. A bot monitoring the Solana mempool or watching for new program deployments can detect the program ID and immediately submit a front-running `initialize` transaction with a higher priority fee. This is a well-known attack pattern on Solana programs that separate deployment from initialization. No privileged access, leaked keys, or oracle collusion is required — only the ability to send a transaction.

---

### Recommendation

Restrict `initialize` to the program's upgrade authority using Anchor's `upgrade_authority_address` constraint, as recommended in the Anchor documentation:

```rust
#[derive(Accounts)]
#[instruction(initial_config: Config)]
pub struct Initialize<'info> {
    #[account(mut, constraint = payer.key() == program_data.upgrade_authority_address.unwrap_or_default())]
    pub payer: Signer<'info>,
    #[account(
        seeds = [ID.as_ref()],
        bump,
        seeds::program = bpf_loader_upgradeable::id(),
    )]
    pub program_data: Account<'info, ProgramData>,
    #[account(init, space = Config::LEN, payer = payer, seeds = [CONFIG_SEED.as_ref()], bump)]
    pub config: Account<'info, Config>,
    pub system_program: Program<'info, System>,
}
```

Apply the same fix to `pyth-price-store`'s `initialize` processor. [5](#0-4) 

---

### Proof of Concept

1. Pyth team deploys `pyth-solana-receiver` program on Solana.
2. Attacker monitors the chain for the new program ID.
3. Before the deployer calls `initialize`, attacker submits:
   ```
   initialize(
     initial_config = Config {
       governance_authority: attacker_pubkey,
       wormhole: attacker_fake_wormhole_program,
       valid_data_sources: [attacker_emitter],
       minimum_signatures: 1,
       single_update_fee_in_lamports: 0,
       target_governance_authority: None,
     }
   )
   ```
   with accounts `{ payer: attacker, config: PDA(CONFIG_SEED), system_program }`.
4. Anchor's `init` constraint creates the config PDA with attacker-controlled values.
5. Deployer's `initialize` call fails with `AccountAlreadyInitialized` (Anchor `init` constraint).
6. Attacker now calls `set_wormhole_address` (as `governance_authority`) to point to their fake Wormhole program, which accepts any VAA.
7. Attacker posts fake price updates via `post_update_atomic` using their fake Wormhole, producing `PriceUpdateV2` accounts with manipulated prices.
8. Any protocol consuming these price accounts is exploited. [1](#0-0) [2](#0-1) [4](#0-3)

### Citations

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L11-18)
```rust
use {
    crate::error::ReceiverError,
    anchor_lang::prelude::*,
    pyth_solana_receiver_sdk::{
        config::{Config, DataSource},
        pda::{CONFIG_SEED, TREASURY_SEED},
        price_update::{PriceUpdateV2, TwapUpdate, VerificationLevel},
        PostTwapUpdateParams, PostUpdateAtomicParams, PostUpdateParams,
```

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

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L17-46)
```rust
pub fn initialize(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    args: &InitializeArgs,
) -> ProgramResult {
    let mut accounts = accounts.iter();
    let payer = validate_payer(accounts.next())?;
    let config = validate_config(accounts.next(), args.config_bump, program_id, true)?;
    let system = validate_system(accounts.next())?;

    let lamports = (Rent::get()?).minimum_balance(accounts::config::SIZE);

    invoke_signed(
        &system_instruction::create_account(
            payer.key,
            config.key,
            lamports,
            accounts::config::SIZE
                .try_into()
                .expect("unexpected overflow"),
            program_id,
        ),
        &[payer.clone(), config.clone(), system.clone()],
        &[&[CONFIG_SEED.as_bytes(), &[args.config_bump]]],
    )?;

    accounts::config::create(*config.data.borrow_mut(), args.authority)?;

    Ok(())
}
```
