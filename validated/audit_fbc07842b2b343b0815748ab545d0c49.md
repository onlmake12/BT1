### Title
Unprotected `initialize` Allows Frontrunner to Seize Control of Pyth Solana Receiver - (`target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

### Summary
The `initialize` instruction of the Pyth Solana Receiver program has no access-control constraint on the `payer` signer. Any unprivileged transaction sender can call it before the legitimate deployer and supply a malicious `initial_config`, permanently seizing `governance_authority` and the trusted `wormhole` address for the entire receiver.

### Finding Description
The `Initialize` account struct in the Pyth Solana Receiver program defines the one-time initialization entry point:

```rust
#[derive(Accounts)]
#[instruction(initial_config : Config)]
pub struct Initialize<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(init, space = Config::LEN, payer=payer,
              seeds = [CONFIG_SEED.as_ref()], bump)]
    pub config: Account<'info, Config>,
    pub system_program: Program<'info, System>,
}
``` [1](#0-0) 

The `payer` field carries only `Signer<'info>` — there is no constraint tying it to the program's upgrade authority, a deployer PDA, or any other privileged key. The `config` PDA is derived from the fixed seed `CONFIG_SEED`, so its address is fully deterministic and publicly computable before the program is ever deployed. Anchor's `init` constraint ensures the account can be created exactly once; after that, every subsequent call to `initialize` reverts.

The `initial_config: Config` argument is stored verbatim. It contains, at minimum, `governance_authority` (the key that can later call all governance instructions) and `wormhole` (the program used to verify every incoming VAA / price update). [2](#0-1) 

The `Governance` struct enforces `payer.key() == config.governance_authority`, so whoever wins the initialization race permanently owns all subsequent governance actions.

### Impact Explanation
An attacker who initializes the config first can supply:

- **Malicious `governance_authority`**: The attacker becomes the sole entity that can call `requestGovernanceAuthorityTransfer`, `acceptGovernanceAuthorityTransfer`, and all other governance instructions. They can later rotate the `wormhole` address to a contract they control.
- **Malicious `wormhole` address**: VAA signature verification is delegated entirely to this address. A fake Wormhole contract can approve arbitrary price-update VAAs, allowing the attacker to post any price for any feed to every downstream DeFi protocol that reads from the Pyth Solana Receiver.
- **Malicious `data_sources`**: The attacker can whitelist emitter addresses they control, accepting forged price attestations.

The result is complete, permanent compromise of price-feed integrity for all consumers of the Pyth Solana Receiver on Solana — including lending protocols, perpetuals, and options platforms — until the program is redeployed.

### Likelihood Explanation
The attack window opens the moment the program binary is deployed on-chain and closes the instant the legitimate `initialize` transaction is confirmed. On Solana there is no public mempool in the Ethereum sense, but:

1. Program deployment and initialization are almost always separate transactions.
2. Any RPC node that exposes pending transactions (or a validator colluding with the attacker) can observe the `initialize` call and submit a competing one with higher priority fees.
3. Even without mempool visibility, an attacker who monitors the chain for a newly deployed program ID can race to call `initialize` before the deployer does, especially if there is any delay between deployment and initialization (e.g., a multi-step deployment script).

The attack requires no privileged access, no leaked keys, and no governance majority — only the ability to submit a Solana transaction.

### Recommendation
Add a constraint that ties the `payer` to the program's upgrade authority (BPF Upgradeable Loader program-data account) or to a hard-coded deployer address:

```rust
#[derive(Accounts)]
#[instruction(initial_config : Config)]
pub struct Initialize<'info> {
    #[account(mut,
        constraint = payer.key() == program_data.upgrade_authority_address
            .ok_or(ReceiverError::Unauthorized)?
            @ ReceiverError::Unauthorized)]
    pub payer: Signer<'info>,
    #[account(
        seeds = [ID.as_ref()],
        seeds::program = bpf_loader_upgradeable::ID,
        bump
    )]
    pub program_data: Account<'info, ProgramData>,
    #[account(init, space = Config::LEN, payer=payer,
              seeds = [CONFIG_SEED.as_ref()], bump)]
    pub config: Account<'info, Config>,
    pub system_program: Program<'info, System>,
}
```

Alternatively, perform deployment and initialization atomically in a single transaction so no window exists.

### Proof of Concept

1. Attacker monitors the Solana cluster for a new deployment of the Pyth Solana Receiver program ID.
2. Immediately after the program binary appears on-chain (but before the deployer's `initialize` transaction lands), the attacker submits:
   ```
   initialize(initial_config = Config {
       governance_authority: attacker_pubkey,
       wormhole: attacker_fake_wormhole_program,
       data_sources: [...],
       valid_time_period_seconds: u64::MAX,
       single_update_fee_in_lamports: 0,
   })
   ```
   with a high priority fee to ensure it lands first.
3. The config PDA (`seeds = [CONFIG_SEED]`) is now owned by the attacker's parameters.
4. The legitimate deployer's `initialize` transaction fails with Anchor's "account already in use" error.
5. The attacker calls `update` on their fake Wormhole program to approve a VAA containing any price they choose, then calls `postUpdate` on the Pyth Receiver — which passes verification because `config.wormhole` points to the attacker's contract.
6. All downstream protocols reading from the Pyth Solana Receiver now consume attacker-controlled prices. [3](#0-2)

### Citations

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
