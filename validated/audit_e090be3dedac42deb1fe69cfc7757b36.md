### Title
Unprotected `initialize` Allows Any Caller to Claim `governance_authority` — (`target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

### Summary
The `pyth_solana_receiver::initialize` instruction has no access control on the caller. Any unprivileged account that races to call it before the legitimate deployer can supply an arbitrary `Config`, including setting themselves as `governance_authority` and pointing `wormhole` at an attacker-controlled program.

### Finding Description
The `Initialize` accounts struct requires only that `payer` is a `Signer` — there is no constraint tying the payer to a specific authorized deployer key, and no constraint that `initial_config.governance_authority` must equal the payer or any hardcoded Pyth key. [1](#0-0) 

The instruction body performs only a `minimum_signatures > 0` check before writing the caller-supplied `Config` verbatim into the PDA: [2](#0-1) 

Because the config PDA uses `init` (not `init_if_needed`), it can be created exactly once. Whoever calls `initialize` first owns the resulting `Config`. All subsequent governance instructions (`set_wormhole_address`, `set_data_sources`, `set_fee`, `request_governance_authority_transfer`) gate on `payer.key() == config.governance_authority`: [3](#0-2) 

### Impact Explanation
An attacker who wins the race gains full `governance_authority`. They can:
- Call `set_wormhole_address` to redirect VAA verification to an attacker-controlled program, bypassing guardian-signature checks entirely.
- Call `set_data_sources` to accept price updates from arbitrary emitters.
- Call `set_fee` to drain user funds or set fees to zero.
- Call `request_governance_authority_transfer` to permanently transfer control.

The legitimate deployer's subsequent `initialize` call will fail with an "account already in use" error because the PDA already exists.

### Likelihood Explanation
The attack window is the gap between program deployment and the deployer's `initialize` transaction. On Solana, transactions are public in the mempool and block explorers index program deployments in real time. A monitoring bot can detect the deployment and submit a front-running `initialize` in the same or next slot. The attack requires no privileged access, no leaked keys, and no off-chain collusion — only the ability to submit a transaction.

### Recommendation
Add a constraint that ties initialization to a specific, hardcoded deployer key, or require that `initial_config.governance_authority == payer.key()` so that at minimum the caller must prove ownership of the key they are installing as governor. The most robust fix is to hardcode the expected governance authority as a program constant and enforce it in the `Initialize` constraint:

```rust
#[account(init, ..., seeds = [CONFIG_SEED.as_ref()], bump)]
pub config: Account<'info, Config>,
// add:
#[account(constraint = payer.key() == EXPECTED_GOVERNANCE_AUTHORITY)]
pub payer: Signer<'info>,
```

Alternatively, use a two-step pattern: initialize with `payer` as a temporary authority, then immediately transfer governance to the intended key in the same deployment script — but this still leaves a race window and is inferior to a hardcoded check.

### Proof of Concept
```rust
// attacker_keypair is any funded keypair
let attacker_config = Config {
    governance_authority: attacker_keypair.pubkey(),
    target_governance_authority: None,
    wormhole: attacker_wormhole_program,
    valid_data_sources: vec![DataSource { chain: 1, emitter: attacker_emitter }],
    single_update_fee_in_lamports: 0,
    minimum_signatures: 1,
};

// Submit before deployer's initialize tx
program_simulator.process_ix(
    Initialize::populate(&attacker_keypair.pubkey(), attacker_config),
    &[&attacker_keypair],
).await.unwrap();

// Verify attacker owns governance
let config = program_simulator.get_anchor_account_data::<Config>(get_config_address()).await.unwrap();
assert_eq!(config.governance_authority, attacker_keypair.pubkey());

// Attacker redirects VAA verification
program_simulator.process_ix(
    SetWormholeAddress::populate(&attacker_keypair.pubkey(), attacker_wormhole_program),
    &[&attacker_keypair],
).await.unwrap();
```

The deployer's subsequent `initialize` call fails because the config PDA already exists, and the attacker retains permanent governance control. [4](#0-3)

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

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L307-315)
```rust
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
