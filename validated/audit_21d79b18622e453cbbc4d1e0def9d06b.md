### Title
Unprivileged Initialization Front-Run Allows Permanent Authority Lockout - (`target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs`)

### Summary
The `initialize` instruction accepts an arbitrary `authority` field from any signer with no validation that the authority is a live, controllable key. Because the config PDA can only be initialized once, an attacker who calls `Initialize` before the legitimate deployer — passing `authority = [0u8;32]` — permanently bricks `InitializePublisher` for the lifetime of the program.

### Finding Description

The `initialize` processor validates only that the payer is a signer and writable: [1](#0-0) 

It then writes whatever 32-byte value was supplied in `args.authority` directly into the config PDA: [2](#0-1) 

There is no check that the payer equals the authority, and no check that the authority is a non-zero or otherwise reachable pubkey.

The config account is a singleton PDA. Its `create` function rejects any second call with `AlreadyInitialized`: [3](#0-2) 

`validate_authority`, called by every `InitializePublisher` invocation, requires the transaction signer's pubkey to exactly match `config.authority`: [4](#0-3) 

If `config.authority` is `[0u8;32]` (the system program's address), no private key exists that can produce a valid signature for that pubkey, so `validate_authority` will always return `MissingRequiredSignature`.

### Impact Explanation

Every publisher registration flows through `InitializePublisher` → `validate_authority`. With a dead authority stored in the config, no publisher can ever have a `publisher_config` or `buffer` account created. This permanently blocks all publisher yield and price-feed participation for the deployed program instance.

### Likelihood Explanation

The vulnerability is exploitable in the window between program deployment and the legitimate `Initialize` call. On Solana, transactions are public in the mempool; an attacker can observe the deployment transaction and immediately submit their own `Initialize` with a dead authority. The attacker needs only a funded keypair — no privileged access is required.

### Recommendation

Add a check inside `initialize` that the payer is also the authority (or that the authority has co-signed the transaction):

```rust
// In initialize.rs, after validate_payer:
ensure!(
    ProgramError::MissingRequiredSignature,
    payer.key.to_bytes() == args.authority
);
```

Alternatively, require the authority account to be passed as an explicit signer in the account list and validate `authority.is_signer` before writing it to the config.

### Proof of Concept

Call sequence:
1. Attacker submits `Initialize` with `authority = [0u8; 32]`, signed by any funded keypair as payer.
2. Config PDA is created with `config.authority = [0u8; 32]`.
3. Legitimate operator submits `InitializePublisher` with their real keypair as authority.
4. `validate_authority` evaluates `authority.key.to_bytes() == config.authority` → `real_key != [0u8;32]` → returns `MissingRequiredSignature`.
5. Step 4 repeats for every publisher, forever, because the config cannot be re-initialized. [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L17-45)
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
```

**File:** target_chains/solana/programs/pyth-price-store/src/accounts/config.rs (L43-44)
```rust
    if data.format != 0 {
        return Err(ReadAccountError::AlreadyInitialized);
```

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L75-78)
```rust
    ensure!(
        ProgramError::MissingRequiredSignature,
        authority.key.to_bytes() == config.authority
    );
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs (L36-39)
```rust
    let mut accounts = accounts.iter();
    let first_account = accounts.next();
    let config = validate_config(accounts.next(), args.config_bump, program_id, false)?;
    let authority = validate_authority(first_account, config)?;
```
