### Title
Unprivileged Front-Run of `Initialize` Allows Attacker to Permanently Seize `config.authority` — (`target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs`)

---

### Summary

The `Initialize` instruction has no access control on the caller. Any arbitrary signer can invoke it before the legitimate deployer, supply an attacker-controlled pubkey as `args.authority`, and permanently lock publisher onboarding.

---

### Finding Description

`initialize()` calls `validate_payer()` as its only caller check: [1](#0-0) 

`validate_payer()` only verifies `is_signer` and `is_writable` — there is no check that the payer equals the program upgrade authority or any other privileged key: [2](#0-1) 

`args.authority` is a raw `[u8; 32]` supplied entirely by the caller in instruction data: [3](#0-2) 

`config::create()` writes whatever `authority` bytes were passed in, with no validation of their origin: [4](#0-3) 

The only guard is the "already initialized" check (`data.format != 0`), which means **whoever calls `Initialize` first wins**: [5](#0-4) 

---

### Impact Explanation

Once the config PDA is initialized with the attacker's authority, `validate_authority()` enforces that only the attacker's key can sign `InitializePublisher`: [6](#0-5) 

The legitimate deployer's subsequent `Initialize` call fails with `AlreadyInitialized`. No publisher can ever be onboarded without the attacker's cooperation. All downstream price submission is permanently frozen unless the program is redeployed (requiring upgrade authority).

---

### Likelihood Explanation

Solana has no public mempool, but the attack window is the gap between program deployment and the deployer's `Initialize` transaction landing. An attacker monitoring on-chain program deployments (e.g., via a validator or RPC subscription) can race this window. The attack requires only a funded keypair and knowledge of the program ID — no privileged access whatsoever.

---

### Recommendation

Bind `Initialize` to a specific privileged signer. The canonical Solana pattern is to check that the payer equals the program's upgrade authority:

```rust
// In initialize(), after validate_payer():
let program_data = /* load program data account */;
let upgrade_authority = /* read upgrade_authority_address from program data */;
ensure!(
    ProgramError::MissingRequiredSignature,
    payer.key == &upgrade_authority
);
```

Alternatively, hard-code the expected deployer pubkey as a program constant and check `payer.key == &EXPECTED_DEPLOYER`.

---

### Proof of Concept

```rust
// 1. Deploy the program (program_id known).
// 2. Attacker submits Initialize before deployer:
let attacker_authority = attacker_keypair.pubkey();
let (config_pda, config_bump) =
    Pubkey::find_program_address(&[CONFIG_SEED.as_bytes()], &program_id);
let mut data = vec![Instruction::Initialize as u8, config_bump];
data.extend_from_slice(&attacker_authority.to_bytes());
// sign with attacker_keypair, submit — succeeds.

// 3. Deployer's Initialize now returns AlreadyInitialized.
// 4. config.authority == attacker_authority.
// 5. All InitializePublisher calls require attacker's signature.
``` [7](#0-6)

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

**File:** target_chains/solana/programs/pyth-price-store/src/instruction.rs (L49-55)
```rust
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
