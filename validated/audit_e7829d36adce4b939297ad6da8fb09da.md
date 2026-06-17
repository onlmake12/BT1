### Title
Griefing `initialize_publisher` via Pre-Funding the Publisher Config PDA - (File: `target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs`)

---

### Summary

The `initialize_publisher` function in the `pyth-price-store` Solana program calls `system_instruction::create_account` to allocate the publisher config PDA without first checking whether the target account already has lamports. Because the PDA address is fully deterministic (derived from a constant seed + publisher pubkey), any unprivileged attacker can compute it and send even 1 lamport to that address. Solana's system program will then reject the subsequent `create_account` call with `AccountAlreadyInUse`, permanently blocking initialization of that publisher's config under the current program logic.

---

### Finding Description

In `initialize_publisher.rs`, the function computes the publisher config PDA and immediately calls `system_instruction::create_account`:

```rust
invoke_signed(
    &system_instruction::create_account(
        authority.key,
        publisher_config.key,   // <-- PDA address
        lamports,
        publisher_config::SIZE.try_into()...,
        program_id,
    ),
    &[authority.clone(), publisher_config.clone(), system.clone()],
    &[&[PUBLISHER_CONFIG_SEED.as_bytes(), &args.publisher, &[args.publisher_config_bump]]],
)?;
``` [1](#0-0) 

Solana's `system_instruction::create_account` fails with `AccountAlreadyInUse` if the target account has **any** lamports, even if it has zero data. The PDA address is deterministic:

```
PDA = find_program_address([PUBLISHER_CONFIG_SEED, publisher_pubkey], program_id)
```

An attacker can compute this address for any publisher and send 1 lamport to it via a simple SOL transfer before the authority calls `initialize_publisher`. The `create_account` CPI will then revert, and there is no fallback path in the current code.

The same pattern exists in `initialize.rs` for the global config PDA: [2](#0-1) 

By contrast, the `message_buffer` program correctly handles this case by checking `is_uninitialized_account` and using transfer + allocate + assign instead of `create_account`: [3](#0-2) 

The Wormhole core-bridge utility `create_account_safe` also demonstrates the correct pattern: [4](#0-3) 

---

### Impact Explanation

**Impact: Medium**

- Any attacker can permanently block the initialization of any publisher's config PDA by sending 1 lamport to the deterministic PDA address.
- Without a publisher config, the publisher cannot call `submit_prices`, preventing that publisher from contributing price data to the Pyth oracle.
- The authority has no in-protocol workaround; a program upgrade would be required to recover.
- The global config PDA (`initialize.rs`) is similarly vulnerable, but is a one-time operation likely already executed on mainnet.

---

### Likelihood Explanation

**Likelihood: Medium**

- The PDA address is fully deterministic and can be computed by anyone given the publisher's public key and the program ID.
- The cost of the attack is negligible (1 lamport + transaction fee).
- An attacker monitoring the mempool or governance channels for upcoming publisher additions can front-run the `initialize_publisher` call.
- The attack is silent and requires no special privileges.

---

### Recommendation

Replace `system_instruction::create_account` with the safe pattern already used elsewhere in the codebase (e.g., `create_account_safe` from the core-bridge utilities, or the transfer + allocate + assign pattern from `create_buffer`). Before calling `create_account`, check whether the account already has lamports and, if so, use `allocate` + `assign` + a top-up transfer instead:

```rust
if publisher_config.lamports() == 0 {
    invoke_signed(&system_instruction::create_account(...), ...)?;
} else {
    // transfer top-up if needed, then allocate + assign
    invoke_signed(&system_instruction::allocate(...), ...)?;
    invoke_signed(&system_instruction::assign(...), ...)?;
}
```

---

### Proof of Concept

1. Observe the `pyth-price-store` program ID and compute the publisher config PDA for a target publisher pubkey `P`:
   ```
   PDA = find_program_address(["publisher_config", P.to_bytes()], program_id)
   ```
2. Send 1 lamport to `PDA` via a standard SOL transfer (no special permissions required).
3. The authority calls `initialize_publisher` for publisher `P`.
4. The `system_instruction::create_account` CPI fails with `AccountAlreadyInUse` because `PDA.lamports() > 0`.
5. Publisher `P` can never be initialized under the current program logic; price submissions from `P` are permanently blocked.

### Citations

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs (L53-69)
```rust
    invoke_signed(
        &system_instruction::create_account(
            authority.key,
            publisher_config.key,
            lamports,
            publisher_config::SIZE
                .try_into()
                .expect("unexpected overflow"),
            program_id,
        ),
        &[authority.clone(), publisher_config.clone(), system.clone()],
        &[&[
            PUBLISHER_CONFIG_SEED.as_bytes(),
            &args.publisher,
            &[args.publisher_config_bump],
        ]],
    )?;
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L29-41)
```rust
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
```

**File:** pythnet/message_buffer/programs/message_buffer/src/instructions/create_buffer.rs (L40-74)
```rust
    if is_uninitialized_account(buffer_account) {
        let (pda, bump) = Pubkey::find_program_address(
            &[
                allowed_program_auth.as_ref(),
                MESSAGE.as_bytes(),
                base_account_key.as_ref(),
            ],
            &crate::ID,
        );
        require_keys_eq!(buffer_account.key(), pda);
        let signer_seeds = [
            allowed_program_auth.as_ref(),
            MESSAGE.as_bytes(),
            base_account_key.as_ref(),
            &[bump],
        ];

        CreateBuffer::create_account(
            buffer_account,
            target_size as usize,
            &ctx.accounts.payer,
            &[signer_seeds.as_slice()],
            &ctx.accounts.system_program,
        )?;

        let loader =
            AccountLoader::<MessageBuffer>::try_from_unchecked(&crate::ID, buffer_account)?;
        {
            let mut message_buffer = loader.load_init()?;
            *message_buffer = MessageBuffer::new(bump);
        }
        loader.exit(&crate::ID)?;
    } else {
        msg!("Buffer account already initialized");
    }
```

**File:** target_chains/solana/programs/core-bridge/src/utils/cpi.rs (L44-72)
```rust
pub fn create_account_safe<'info>(
    ctx: CpiContext<'_, '_, '_, 'info, CreateAccountSafe<'info>>,
    data_len: usize,
    owner: &Pubkey,
) -> Result<()> {
    // If the account being initialized already has lamports, then we need to send an amount of
    // lamports to the account to cover rent, allocate space and then assign to the owner.
    // Otherwise, we use the create account instruction.
    //
    // NOTE: This was taken from Anchor's create account handling.
    let current_lamports = ctx.accounts.new_account.lamports();
    if current_lamports == 0 {
        system_program::create_account(
            CpiContext::new_with_signer(
                ctx.program_id,
                system_program::CreateAccount {
                    from: ctx.accounts.payer,
                    to: ctx.accounts.new_account,
                },
                ctx.signer_seeds,
            ),
            Rent::get().map(|rent| rent.minimum_balance(data_len))?,
            data_len.try_into().unwrap(),
            owner,
        )
    } else {
        allocate_and_assign_account(ctx, data_len, owner, current_lamports)
    }
}
```
