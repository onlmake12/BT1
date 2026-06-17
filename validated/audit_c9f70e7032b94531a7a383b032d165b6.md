### Title
Unprotected `initialize` Entrypoint Allows Attacker to Seize Authority and Inject Fabricated Oracle Prices — (`target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs`)

---

### Summary

The `initialize` instruction that bootstraps the singleton config PDA has **no caller access control**. Any account can invoke it before the legitimate deployer, supply an attacker-controlled `authority` pubkey, and permanently own the program's authority role. From that position the attacker can register arbitrary publishers via `InitializePublisher` and write fabricated prices via `submit_prices`, with every on-chain guard passing cleanly.

---

### Finding Description

`initialize` accepts the `authority` field as a caller-supplied argument and validates the payer only for `is_signer` and `is_writable`: [1](#0-0) 

There is no check that the payer is a specific trusted account, and `authority` is written verbatim from `args.authority`: [2](#0-1) 

The config PDA is a singleton derived from a fixed seed. Once written, `config::create` rejects any second call with `AlreadyInitialized`: [3](#0-2) 

So the first caller wins permanently.

After seizing authority, `InitializePublisher` only checks that the signer matches `config.authority`: [4](#0-3) 

The attacker passes this check trivially, registers any publisher pubkey they control, and links it to a buffer account they pre-created and transferred ownership of to the program.

`submit_prices` then verifies only that (a) the publisher is a signer, (b) the publisher config PDA is correctly derived from that publisher key, and (c) the buffer key stored in the config matches the provided buffer: [5](#0-4) 

All three checks pass for the attacker's accounts. Price values are explicitly **not validated** before being written: [6](#0-5) 

---

### Impact Explanation

An attacker who front-runs `initialize` gains permanent, irrevocable authority over the program (the config PDA cannot be re-initialized). They can register an unlimited number of attacker-controlled publishers and write arbitrary `price`, `confidence`, and `trading_status` values into the oracle feed. These values are indistinguishable from legitimate publisher data at the buffer level and will be consumed by the validator as authoritative price updates.

---

### Likelihood Explanation

On Solana, program deployment and initialization are separate transactions. An attacker monitoring the chain for the program's deployment transaction can submit `initialize` in the very next block with a higher priority fee. The window is small but deterministic and requires no privileged access, no leaked keys, and no off-chain coordination — only a monitoring script and a funded keypair.

---

### Recommendation

Restrict `initialize` to a known, hard-coded deployer pubkey, or require the payer to sign and derive `authority` from the payer rather than accepting it as a free argument. A minimal fix is to add a compile-time constant for the expected initializer and assert `payer.key == &EXPECTED_INITIALIZER` inside `initialize` before writing the config. Alternatively, pass the upgrade authority of the program as a required signer so that only the program's upgrade authority can call `initialize`.

---

### Proof of Concept

The existing integration test in `initialize_publisher.rs` already demonstrates the complete three-step flow (initialize → initialize_publisher → submit_prices) with a single keypair acting as both payer and authority. [7](#0-6) 

To reproduce the exploit, replace the `authority` keypair in that test with a fresh attacker keypair, submit `initialize` before the legitimate deployer, and observe that fabricated prices (e.g., `price = i64::MAX`) appear verbatim in the buffer and pass all on-chain validation.

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

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L43-45)
```rust
    accounts::config::create(*config.data.borrow_mut(), args.authority)?;

    Ok(())
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

**File:** target_chains/solana/programs/pyth-price-store/src/processor/submit_prices.rs (L29-49)
```rust
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
```

**File:** target_chains/solana/programs/pyth-price-store/src/accounts/buffer.rs (L172-173)
```rust
    // We don't validate the values of `new_prices` to make the publishing process
    // more efficient. They will be validated when applied in the validator.
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs (L113-273)
```rust
    #[tokio::test]
    async fn test_initialize_and_publish() {
        let id = Pubkey::new_unique();
        let (mut banks_client, authority, recent_blockhash) = ProgramTest::new(
            "publishers",
            id,
            processor!(crate::processor::process_instruction),
        )
        .start()
        .await;

        // Setup Accounts
        let (config, config_bump) = Pubkey::find_program_address(&[CONFIG_SEED.as_bytes()], &id);

        let publisher = Keypair::new();

        let (publisher_config, publisher_config_bump) = Pubkey::find_program_address(
            &[
                PUBLISHER_CONFIG_SEED.as_bytes(),
                &publisher.pubkey().to_bytes(),
            ],
            &id,
        );

        // First we need to initialize the vault PDA for use in the next instruction.
        let mut data = vec![
            crate::instruction::Instruction::Initialize as u8,
            config_bump,
        ];
        data.extend_from_slice(&authority.pubkey().to_bytes());
        let mut transaction = Transaction::new_with_payer(
            &[Instruction {
                program_id: id,
                data,
                accounts: vec![
                    AccountMeta::new_readonly(authority.pubkey(), true),
                    AccountMeta::new(config, false),
                    AccountMeta::new_readonly(system_program::id(), false),
                ],
            }],
            Some(&authority.pubkey()),
        );
        transaction.sign(&[&authority], recent_blockhash);
        banks_client.process_transaction(transaction).await.unwrap();

        // Create a buffer account.
        let buffer_space = accounts::buffer::size(5000);
        let buffer_lamports = Rent::default().minimum_balance(buffer_space);
        let buffer_key = Pubkey::create_with_seed(&authority.pubkey(), "seed1", &id).unwrap();
        let mut transaction = Transaction::new_with_payer(
            &[
                solana_program::system_instruction::create_account_with_seed(
                    &authority.pubkey(),
                    &buffer_key,
                    &authority.pubkey(),
                    "seed1",
                    buffer_lamports,
                    buffer_space as u64,
                    &id,
                ),
            ],
            Some(&authority.pubkey()),
        );
        transaction.sign(&[&authority], recent_blockhash);
        banks_client.process_transaction(transaction).await.unwrap();

        // Create a publisher's buffer account.
        let mut data = vec![
            crate::instruction::Instruction::InitializePublisher as u8,
            config_bump,
            publisher_config_bump,
        ];
        data.extend_from_slice(&publisher.pubkey().to_bytes());
        let mut transaction = Transaction::new_with_payer(
            &[Instruction {
                program_id: id,
                data,
                accounts: vec![
                    AccountMeta::new(authority.pubkey(), true),
                    AccountMeta::new_readonly(config, false),
                    AccountMeta::new(publisher_config, false),
                    AccountMeta::new(buffer_key, false),
                    AccountMeta::new_readonly(system_program::id(), false),
                ],
            }],
            Some(&authority.pubkey()),
        );
        transaction.sign(&[&authority], recent_blockhash);
        banks_client.process_transaction(transaction).await.unwrap();

        {
            // Validate the publisher config PDA allocation.
            let buffer = banks_client
                .get_account(publisher_config)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(buffer.owner, id);
            let actual = accounts::publisher_config::read(&buffer.data).unwrap();
            assert_eq!(actual.buffer_account, buffer_key.to_bytes());
            assert_eq!(actual.publisher, publisher.pubkey().to_bytes());
        }

        {
            let header = BufferHeader::new(publisher.pubkey().to_bytes());
            let header = bytes_of(&header);
            // Validate the buffer initialization.
            let buffer = banks_client.get_account(buffer_key).await.unwrap().unwrap();
            assert_eq!(buffer.owner, id);
            assert_eq!(&buffer.data[..header.len()], header);
        }

        // Topup the publisher account
        let mut transaction = Transaction::new_with_payer(
            &[solana_program::system_instruction::transfer(
                &authority.pubkey(),
                &publisher.pubkey(),
                1_000_000_000,
            )],
            Some(&authority.pubkey()),
        );
        transaction.sign(&[&authority], recent_blockhash);
        banks_client.process_transaction(transaction).await.unwrap();

        // Publish some prices.
        let mut data = vec![
            crate::instruction::Instruction::SubmitPrices as u8,
            publisher_config_bump,
        ];
        let prices = [
            BufferedPrice::new(1, 2, 200, 3).unwrap(),
            BufferedPrice::new(2, 3, 300, 4).unwrap(),
        ];
        data.extend_from_slice(&cast_slice(&prices));
        let mut transaction = Transaction::new_with_payer(
            &[Instruction {
                program_id: id,
                data,
                accounts: vec![
                    AccountMeta::new(publisher.pubkey(), true),
                    AccountMeta::new_readonly(publisher_config, false),
                    AccountMeta::new(buffer_key, false),
                ],
            }],
            Some(&publisher.pubkey()),
        );
        transaction.sign(&[&publisher], recent_blockhash);
        banks_client.process_transaction(transaction).await.unwrap();

        {
            // Validate the Allocation
            let buffer = banks_client.get_account(buffer_key).await.unwrap().unwrap();
            assert_eq!(buffer.owner, id);
            let (out_header, out_prices) = accounts::buffer::read(&buffer.data).unwrap();

            assert_eq!(&out_header.publisher, &publisher.pubkey().to_bytes());
            assert_eq!({ out_header.num_prices }, 2);
            assert_ne!({ out_header.slot }, 0);
            assert_eq!(&prices[..], out_prices);
        }
    }
```
