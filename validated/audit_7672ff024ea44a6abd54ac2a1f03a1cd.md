I have all the information needed. Let me trace the exact attack path through the code.

**Key facts from the code:**

**1. `PostUpdate` struct has no seeds constraint on `price_update_account`:** [1](#0-0) 

The only constraint is `init_if_needed` + the `write_authority` check. Any arbitrary address can be passed as `price_update_account`.

**2. The `write_authority` constraint allows initialization with any signer:** [2](#0-1) 

If `price_update_account.write_authority == Pubkey::default()` (uninitialized), the constraint passes and the account is initialized with `write_authority = write_authority.key()` (attacker's key).

**3. The push oracle sets `write_authority = price_feed_account` (the PDA itself) in its CPI:** [3](#0-2) 

**4. The push oracle only skips the CPI if `next_timestamp <= current_timestamp`:** [4](#0-3) 

If the account is pre-initialized (not `data_is_empty()`), the code reads the stored timestamp and attempts the CPI for any newer price — which then fails with `WrongWriteAuthority`.

**5. Only the stored `write_authority` can close the account via `reclaim_rent`:** [5](#0-4) 

The push oracle has no way to close or reset the account.

---

### Title
Unprivileged attacker can permanently grief any push oracle price feed PDA by pre-initializing it with an arbitrary `write_authority` - (`target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

### Summary
The `PostUpdate` account struct in `pyth-solana-receiver` places no restriction on which address can be used as `price_update_account`. An attacker can compute the push oracle's PDA (`seeds=[shard_id, feed_id]` under the push oracle program) and call `post_update` directly, initializing that PDA with `write_authority = attacker_key`. All subsequent legitimate `update_price_feed` CPIs into `post_update` will fail with `WrongWriteAuthority` because the stored authority is the attacker's key, not the PDA itself.

### Finding Description
The `pyth-push-oracle` derives a deterministic PDA for each `(shard_id, feed_id)` pair:

```rust
// pyth-push-oracle/src/lib.rs:127
#[account(mut, seeds = [&shard_id.to_le_bytes(), &feed_id], bump)]
pub price_feed_account: UncheckedAccount<'info>,
```

When `update_price_feed` CPIs into `post_update`, it passes `write_authority = price_feed_account` (the PDA itself, signing via `signer_seeds`):

```rust
write_authority: ctx.accounts.price_feed_account.to_account_info().clone(),
// ...
let signer_seeds = &[&seeds[..]];
let cpi_context = CpiContext::new_with_signer(cpi_program, cpi_accounts, signer_seeds);
```

However, `pyth-solana-receiver`'s `PostUpdate` struct has no seeds or ownership constraint on `price_update_account` — only `init_if_needed` and a `write_authority` equality check:

```rust
#[account(init_if_needed,
    constraint = price_update_account.write_authority == Pubkey::default()
              || price_update_account.write_authority == write_authority.key()
              @ ReceiverError::WrongWriteAuthority,
    payer = payer, space = PriceUpdateV2::LEN)]
pub price_update_account: Account<'info, PriceUpdateV2>,
pub write_authority: Signer<'info>,
```

An attacker can:
1. Compute the push oracle PDA for any target `(shard_id, feed_id)`.
2. Call `pyth_solana_receiver::post_update` directly, passing that PDA as `price_update_account` and their own key as `write_authority`.
3. Since the account is uninitialized (`write_authority == Pubkey::default()`), the constraint passes and the account is created with `write_authority = attacker_key`.
4. All future `update_price_feed` calls for that feed attempt a CPI with `write_authority = PDA`, which fails the constraint (`attacker_key != PDA`), returning `WrongWriteAuthority`.

The attacker can also set a far-future `publish_time` in the price message, causing `update_price_feed` to silently skip the CPI (`next_timestamp <= current_timestamp`) while the feed is frozen at attacker-controlled stale data.

There is no recovery path: `reclaim_rent` requires `price_update_account.write_authority == payer.key()`, so only the attacker can close the account — and they can immediately re-initialize it.

### Impact Explanation
Permanent, targeted griefing of any specific `(shard_id, feed_id)` price feed. Any protocol relying on that feed via the push oracle will receive no further price updates. The attacker can maintain the freeze indefinitely at negligible cost (only VAA relay fees and rent). This matches the scoped impact: "permanent griefing/freeze of a specific price feed account, preventing any future price publication for that feed."

### Likelihood Explanation
The attack requires no privileged access. Any account with SOL can call `post_update` with a valid VAA (obtainable from the public Wormhole guardian network). The target PDA address is deterministically computable from public parameters. The attack can be executed atomically before any legitimate relayer initializes the account.

### Recommendation
Add a seeds constraint to `price_update_account` in `PostUpdate`, or require that `write_authority` matches a program-controlled authority. Alternatively, the push oracle's `update_price_feed` should verify that `price_feed_account.write_authority == price_feed_account.key()` (or `== Pubkey::default()`) before attempting the CPI, and reject or reclaim the account otherwise. The cleanest fix is to restrict `price_update_account` in `PostUpdate` to only accept accounts whose address matches a PDA derived from the `write_authority`, preventing cross-program PDA squatting.

### Proof of Concept
```
1. Compute push_oracle_pda = Pubkey::find_program_address(
       &[&shard_id.to_le_bytes(), &feed_id],
       &PYTH_PUSH_ORACLE_ID
   ).0

2. Attacker calls pyth_solana_receiver::post_update(
       price_update_account = push_oracle_pda,   // target PDA
       write_authority      = attacker_keypair,  // attacker signs
       encoded_vaa          = <any valid Wormhole VAA>,
       params               = <valid MerklePriceUpdate for any feed>
   )
   → Succeeds: account initialized with write_authority = attacker_key

3. Legitimate relayer calls pyth_push_oracle::update_price_feed(
       price_feed_account = push_oracle_pda,
       shard_id, feed_id, params
   )
   → CPI into post_update fires with write_authority = push_oracle_pda
   → Constraint: attacker_key != Pubkey::default() && attacker_key != push_oracle_pda
   → Returns Err(WrongWriteAuthority)

4. Repeat step 3 indefinitely → feed permanently frozen.
```

### Citations

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L341-344)
```rust
    /// The constraint is such that either the price_update_account is uninitialized or the write_authority is the write_authority.
    /// Pubkey::default() is the SystemProgram on Solana and it can't sign so it's impossible that price_update_account.write_authority == Pubkey::default() once the account is initialized
    #[account(init_if_needed, constraint = price_update_account.write_authority == Pubkey::default() || price_update_account.write_authority == write_authority.key() @ ReceiverError::WrongWriteAuthority , payer =payer, space = PriceUpdateV2::LEN)]
    pub price_update_account: Account<'info, PriceUpdateV2>,
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L400-401)
```rust
    #[account(mut, close = payer, constraint = price_update_account.write_authority == payer.key() @ ReceiverError::WrongWriteAuthority)]
    pub price_update_account: Account<'info, PriceUpdateV2>,
```

**File:** target_chains/solana/programs/pyth-push-oracle/src/lib.rs (L48-56)
```rust
        let cpi_accounts = PostUpdate {
            payer: ctx.accounts.payer.to_account_info().clone(),
            encoded_vaa: ctx.accounts.encoded_vaa.to_account_info().clone(),
            config: ctx.accounts.config.to_account_info().clone(),
            treasury: ctx.accounts.treasury.to_account_info().clone(),
            price_update_account: ctx.accounts.price_feed_account.to_account_info().clone(),
            system_program: ctx.accounts.system_program.to_account_info().clone(),
            write_authority: ctx.accounts.price_feed_account.to_account_info().clone(),
        };
```

**File:** target_chains/solana/programs/pyth-push-oracle/src/lib.rs (L96-97)
```rust
        if next_timestamp > current_timestamp {
            pyth_solana_receiver_sdk::cpi::post_update(cpi_context, params)?;
```
