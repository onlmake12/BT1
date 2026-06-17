### Title
Unpermissioned `initialize()` Allows Front-Run With Garbage Authority, Permanently Freezing the Price Feed Pipeline — (`target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs`)

---

### Summary

`initialize()` has no access control on the caller. Any account can invoke it and supply an arbitrary `authority` value. Because the config PDA can only be initialized once, a front-runner who races the deployer can permanently install an uncontrollable authority, making `initialize_publisher` — and therefore `submit_prices` — permanently uncallable.

---

### Finding Description

**Step 1 — `initialize()` has no caller restriction.**

`validate_payer` only checks `is_signer` and `is_writable`; it does not verify the payer is the program upgrade authority or any specific key. [1](#0-0) 

The instruction itself passes `args.authority` — arbitrary caller-supplied bytes — directly into the config account with no validation. [2](#0-1) 

**Step 2 — Config is write-once.**

`accounts::config::create` checks `data.format != 0` and returns `AlreadyInitialized` if the account already has a valid format magic. Once an attacker's call lands first, the legitimate deployer's call is permanently rejected. [3](#0-2) 

**Step 3 — `initialize_publisher` requires a signature from `config.authority`.**

`validate_authority` reads the authority stored in the config PDA and enforces `authority.key.to_bytes() == config.authority`. If the stored authority is garbage (e.g., all-zero bytes, or a random pubkey with no known private key), this check can never pass. [4](#0-3) 

**Step 4 — `submit_prices` requires a pre-existing `publisher_config` PDA.**

`submit_prices` calls `validate_publisher_config_for_access`, which derives the PDA and reads its contents. If `initialize_publisher` was never called (because the authority is uncontrollable), no `publisher_config` account exists, and every `submit_prices` call fails. [5](#0-4) 

---

### Impact Explanation

The full pipeline `initialize → initialize_publisher → submit_prices` is permanently broken. No publisher can ever write price data. Any downstream consumer of Pyth price feeds on this deployment receives no updates, constituting complete protocol insolvency for that deployment.

---

### Likelihood Explanation

On Solana, program deployment and the first `initialize()` call are separate transactions. An attacker monitoring the chain for the newly deployed program ID can immediately submit their own `Initialize` instruction in the same or next block. No privileged access, leaked key, or social engineering is required — only the ability to submit a transaction.

---

### Recommendation

Restrict `initialize()` so that only the program's upgrade authority can call it. The canonical Solana pattern is to pass the upgrade authority account, derive the `ProgramData` PDA for the program, and verify that the signer matches the upgrade authority stored there. Alternatively, pass the intended authority as a required signer (not just as instruction data), so the legitimate authority must co-sign the initialization transaction, making a front-run useless.

---

### Proof of Concept

```
1. Attacker observes program deployment at <PROGRAM_ID>.
2. Attacker submits Initialize {
       payer:      <attacker_keypair>,   // any funded signer
       config:     PDA(CONFIG_SEED, PROGRAM_ID),
       authority:  [0u8; 32]             // garbage — no one holds this key
   }
3. Transaction lands before the deployer's Initialize call.
4. Deployer's Initialize fails: AlreadyInitialized.
5. Any subsequent InitializePublisher fails: MissingRequiredSignature
   (no one can sign for [0u8;32]).
6. submit_prices fails for every publisher: publisher_config PDA does not exist.
7. Price feed pipeline is permanently frozen.
``` [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** target_chains/solana/programs/pyth-price-store/src/accounts/config.rs (L43-48)
```rust
    if data.format != 0 {
        return Err(ReadAccountError::AlreadyInitialized);
    }
    data.format = FORMAT;
    data.authority = authority;
    Ok(data)
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/submit_prices.rs (L22-56)
```rust
pub fn submit_prices(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    args: &SubmitPricesArgsHeader,
    prices_data: &[u8],
) -> ProgramResult {
    let mut accounts = accounts.iter();
    let publisher = validate_publisher(accounts.next())?;
    let publisher_config = validate_publisher_config_for_access(
        accounts.next(),
        args.publisher_config_bump,
        publisher.key,
        program_id,
    )?;
    let buffer = validate_buffer(accounts.next(), program_id)?;

    let publisher_config_data = publisher_config.data.borrow();
    let publisher_config = publisher_config::read(*publisher_config_data)?;
    // Required to ensure that `find_program_address` returned the same account as
    // `create_program_address` in `initialize_publisher`.
    ensure!(
        ProgramError::InvalidArgument,
        sol_memcmp(&publisher.key.to_bytes(), &publisher_config.publisher, 32) == 0
    );
    ensure!(
        ProgramError::InvalidArgument,
        sol_memcmp(&buffer.key.to_bytes(), &publisher_config.buffer_account, 32) == 0
    );

    // Access and update PublisherPrices account with new data.
    let mut buffer_data = buffer.data.borrow_mut();
    let (header, prices) = buffer::read_mut(*buffer_data)?;
    buffer::update(header, prices, Clock::get()?.slot, prices_data)?;

    Ok(())
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs (L31-87)
```rust
pub fn initialize_publisher(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    args: &InitializePublisherArgs,
) -> ProgramResult {
    let mut accounts = accounts.iter();
    let first_account = accounts.next();
    let config = validate_config(accounts.next(), args.config_bump, program_id, false)?;
    let authority = validate_authority(first_account, config)?;
    let publisher_config = validate_publisher_config_for_init(
        accounts.next(),
        args.publisher_config_bump,
        &args.publisher.into(),
        program_id,
    )?;
    let buffer = validate_buffer(accounts.next(), program_id)?;
    let system = validate_system(accounts.next())?;

    // Deposit enough tokens to allocate the account.
    let rent = Rent::get()?;
    let lamports = rent.minimum_balance(publisher_config::SIZE);

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

    let mut publisher_config_data = publisher_config.data.borrow_mut();
    publisher_config::create(
        *publisher_config_data,
        args.publisher,
        buffer.key.to_bytes(),
    )?;

    // Write an initial Header into the buffer account to prepare it to receive prices.
    let mut buffer_data = buffer.data.borrow_mut();
    ensure!(
        ProgramError::AccountNotRentExempt,
        buffer.lamports() >= rent.minimum_balance(buffer_data.len())
    );
    buffer::create(*buffer_data, args.publisher)?;

    Ok(())
}
```
