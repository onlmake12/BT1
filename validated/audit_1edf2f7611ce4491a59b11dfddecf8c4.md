### Title
Unprivileged First-Caller Can Permanently Hijack Config Authority — (`target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs`)

### Summary
The `Initialize` instruction has no access control on the payer. Any account that can sign a Solana transaction can call it first, supply an arbitrary `authority` value, and permanently own the config PDA — blocking the legitimate operator from ever registering publishers.

---

### Finding Description

`validate_payer` only checks that the account is a signer and writable: [1](#0-0) 

There is no check that the payer is a specific trusted key. The `initialize` processor accepts the `authority` field directly from instruction data and writes it verbatim into the config PDA: [2](#0-1) 

`InitializeArgs.authority` is a raw 32-byte array with no validation: [3](#0-2) 

`accounts::config::create` writes the attacker-supplied bytes directly: [4](#0-3) 

The config PDA is derived from a fixed seed (`CONFIG_SEED`) with no per-deployer salt, so it can only ever be created once. The system program will reject a second `create_account` call on an already-existing account. There is no `update-authority` or re-initialization instruction in the program.

---

### Impact Explanation

`InitializePublisher` enforces that the signer's key equals the stored `config.authority`: [5](#0-4) 

If an attacker front-runs `Initialize` and sets `authority` to their own key (or any key they do not control, e.g. the zero pubkey), the legitimate operator can never satisfy this check. No new publishers can be registered, permanently freezing the oracle's ability to onboard new price feeds.

---

### Likelihood Explanation

- The attacker needs only a funded Solana keypair — no privileged access required.
- The window is the entire period before the legitimate operator's `Initialize` transaction lands on-chain.
- On a public network, a mempool observer or a racing bot can trivially front-run the deployment.
- The damage is irreversible without a program upgrade.

---

### Recommendation

Restrict `Initialize` to a known, hard-coded deployer key or pass the expected authority as a program constant verified at instruction time. At minimum, require that `args.authority` equals `payer.key` so the caller cannot set an authority they do not control. Alternatively, use an upgrade-authority-gated governance instruction to set the authority post-deployment.

---

### Proof of Concept

```
1. Attacker submits Initialize { config_bump, authority: attacker_pubkey }
   → config PDA created, config.authority = attacker_pubkey

2. Legitimate operator submits Initialize { config_bump, authority: operator_pubkey }
   → system_instruction::create_account fails (account already exists)
   → operator cannot overwrite the config

3. Legitimate operator submits InitializePublisher signed by operator_pubkey
   → validate_authority: operator_pubkey.to_bytes() != config.authority (attacker_pubkey)
   → MissingRequiredSignature error

4. Assert: no publisher can ever be registered with the legitimate operator's key.
```

The entire attack is a single transaction on a public RPC endpoint, executable before the program is operationally used.

### Citations

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L32-39)
```rust
pub fn validate_payer<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let payer = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    ensure!(ProgramError::MissingRequiredSignature, payer.is_signer);
    ensure!(ProgramError::InvalidArgument, payer.is_writable);
    Ok(payer)
}
```

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L66-79)
```rust
pub fn validate_authority<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
    config: &AccountInfo<'a>,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let authority = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    ensure!(ProgramError::MissingRequiredSignature, authority.is_signer);
    ensure!(ProgramError::InvalidArgument, authority.is_writable);
    let config_data = config.data.borrow();
    let config = accounts::config::read(*config_data)?;
    ensure!(
        ProgramError::MissingRequiredSignature,
        authority.key.to_bytes() == config.authority
    );
    Ok(authority)
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L23-43)
```rust
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

**File:** target_chains/solana/programs/pyth-price-store/src/accounts/config.rs (L38-48)
```rust
pub fn create(data: &mut [u8], authority: [u8; 32]) -> Result<&mut Config, ReadAccountError> {
    if data.len() < size_of::<Config>() {
        return Err(ReadAccountError::DataTooShort);
    }
    let data: &mut Config = from_bytes_mut(&mut data[..size_of::<Config>()]);
    if data.format != 0 {
        return Err(ReadAccountError::AlreadyInitialized);
    }
    data.format = FORMAT;
    data.authority = authority;
    Ok(data)
```
