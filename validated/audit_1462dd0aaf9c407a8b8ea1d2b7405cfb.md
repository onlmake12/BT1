The code is clear. Let me confirm the exact `is_active` boundary and whether `post_vaa_v1` takes the guardian set account at all.

The TOCTOU gap is confirmed. The new V1 path (`verify_encoded_vaa_v1` → `post_vaa_v1`) omits the guardian-set re-check that the legacy `post_vaa` correctly performs. Here is the full finding:

---

### Title
TOCTOU: Guardian Set Expiry Not Re-Checked in `post_vaa_v1`, Allowing Expired-Set VAAs to Be Permanently Posted — (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/post_vaa_v1.rs`)

---

### Summary
The two-step V1 VAA posting flow (`verify_encoded_vaa_v1` → `post_vaa_v1`) checks guardian-set liveness only in the first step. The second step, `post_vaa_v1`, accepts any `EncodedVaa` whose `status == Verified` without re-validating that the guardian set is still active. An unprivileged relayer can exploit the gap between the two transactions to permanently post a VAA attested by an expired guardian set.

---

### Finding Description

**Step 1 — `verify_encoded_vaa_v1`** checks liveness at call time: [1](#0-0) 

If the check passes, the function sets `header.status = ProcessingStatus::Verified` and writes it back to the `EncodedVaa` account. [2](#0-1) 

**Step 2 — `post_vaa_v1`** accepts the `EncodedVaa` with only a `Verified` status check. The `PostVaaV1` account struct contains **no guardian set account**, and the `constraints` function performs **no liveness re-check**: [3](#0-2) [4](#0-3) 

**Contrast with the legacy path** — the legacy `post_vaa` explicitly re-checks `is_active` at posting time: [5](#0-4) 

**`is_active` boundary** — the guardian set is considered active when `expiration_time >= timestamp` (inclusive), so it expires at `expiration_time + 1`: [6](#0-5) 

The attack window is therefore a single Unix-second boundary: call `verify_encoded_vaa_v1` at timestamp `T` (where `T == expiration_time`, still active), then call `post_vaa_v1` at timestamp `T+1` (expired).

---

### Impact Explanation
A `PostedVaaV1` account is created on-chain whose `guardian_set_index` refers to an expired (potentially compromised) guardian set. Downstream Pyth consumers that rely on posted VAA accounts — including price-feed integrators — will accept this account as authoritative. If the old guardian set was rotated out due to key compromise, an attacker holding a VAA signed by those compromised keys can inject arbitrary price data permanently into the on-chain state.

---

### Likelihood Explanation
Guardian set rotations are infrequent but do occur. The attack requires:
1. A VAA legitimately signed by the old guardian set (freely available from the Wormhole gossip network).
2. Timing `verify_encoded_vaa_v1` to land in the last slot before expiry and `post_vaa_v1` in the first slot after — a one-second window that is straightforward to target by monitoring the on-chain `expiration_time` field.
3. No privileged access; any payer/relayer can execute both instructions.

---

### Recommendation
Add a guardian-set liveness re-check inside `post_vaa_v1`. The simplest fix is to require the guardian set account in `PostVaaV1`, read the `guardian_set_index` from the encoded VAA, and assert `guardian_set.is_active(&Clock::get()?)` before creating the `PostedVaaV1` account — mirroring the guard already present in the legacy `post_vaa` handler.

---

### Proof of Concept

```
1. Deploy core-bridge in a local test validator.
2. Create a guardian set with expiration_time = now + 2.
3. Write a valid VAA (signed by that guardian set) into an EncodedVaa account.
4. At unix_timestamp == expiration_time (slot N):
       call verify_encoded_vaa_v1 → succeeds, EncodedVaa.status = Verified.
5. Advance the validator clock by 1 second (slot N+1, unix_timestamp = expiration_time + 1).
6. Call post_vaa_v1 → succeeds, PostedVaaV1 account is created.
7. Assert: PostedVaaV1.guardian_set_index == expired set index.
8. Assert: guardian_set.is_active(current_timestamp) == false.
```

The posted VAA account exists on-chain despite the guardian set being expired at the time of posting, violating the invariant enforced by the legacy path.

### Citations

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/verify_encoded_vaa_v1.rs (L37-47)
```rust
    fn constraints(ctx: &Context<Self>) -> Result<()> {
        // Guardian set must be active.
        let timestamp = Clock::get().map(Into::into)?;
        require!(
            ctx.accounts.guardian_set.inner().is_active(&timestamp),
            CoreBridgeError::GuardianSetExpired
        );

        // Done.
        Ok(())
    }
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/verify_encoded_vaa_v1.rs (L107-115)
```rust
    // Revise the header.
    header.status = ProcessingStatus::Verified;
    header.version = 1;

    // Finally serialize.
    let acc_data: &mut [_] = &mut ctx.accounts.draft_vaa.data.borrow_mut();
    let mut writer = std::io::Cursor::new(acc_data);
    writer.write_all(<EncodedVaa as anchor_lang::Discriminator>::DISCRIMINATOR)?;
    header.serialize(&mut writer).map_err(Into::into)
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/post_vaa_v1.rs (L8-54)
```rust
#[derive(Accounts)]
pub struct PostVaaV1<'info> {
    /// Payer to create the posted VAA account. This instruction allows anyone with an encoded VAA
    /// to create a posted VAA account.
    #[account(mut)]
    payer: Signer<'info>,

    /// Encoded VAA, whose body will be serialized into the posted VAA account.
    ///
    /// NOTE: This instruction handler only exists to support integrators that still rely on posted
    /// VAA accounts. While we encourage integrators to use the encoded VAA account instead, we
    /// allow a pathway to convert the encoded VAA into a posted VAA. However, the payload is
    /// restricted to 9.5KB, which is much larger than what was possible with the old implementation
    /// using the legacy post vaa instruction. The Core Bridge program will not support posting VAAs
    /// larger than this payload size.
    #[account(
        constraint = encoded_vaa.status == ProcessingStatus::Verified @ CoreBridgeError::UnverifiedVaa
    )]
    encoded_vaa: Account<'info, EncodedVaa>,

    #[account(
        init,
        payer = payer,
        space = PostedVaaV1::compute_size(
            encoded_vaa
                .as_vaa()?
                .to_v1()?
                .body()
                .payload()
                .as_ref()
                .len()
        ),
        seeds = [
            PostedVaaV1::SEED_PREFIX,
            solana_program::keccak::hash(
                encoded_vaa
                    .as_vaa()?
                    .to_v1()?
                    .body().as_ref()
            ).as_ref()
        ],
        bump,
    )]
    posted_vaa: Account<'info, LegacyAnchorized<PostedVaaV1>>,

    system_program: Program<'info, System>,
}
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/post_vaa_v1.rs (L56-71)
```rust
impl<'info> PostVaaV1<'info> {
    fn constraints(ctx: &Context<Self>) -> Result<()> {
        // CPI to create the posted VAA account will fail if the size of the VAA payload is too large.
        require!(
            ctx.accounts.encoded_vaa.buf.len() <= 9_728,
            CoreBridgeError::PostedVaaPayloadTooLarge
        );

        let encoded_vaa = ctx.accounts.encoded_vaa.as_vaa()?;
        encoded_vaa
            .v1()
            .ok_or(error!(CoreBridgeError::InvalidVaaVersion))?;

        // Done.
        Ok(())
    }
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_vaa.rs (L91-99)
```rust
    pub fn constraints(ctx: &Context<Self>, args: &PostVaaArgs) -> Result<()> {
        let guardian_set = ctx.accounts.guardian_set.inner();

        // Check that the guardian set is still active.
        let timestamp = Clock::get().map(Into::into)?;
        require!(
            guardian_set.is_active(&timestamp),
            CoreBridgeError::GuardianSetExpired
        );
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/state/guardian_set.rs (L57-65)
```rust
    pub fn is_active(&self, timestamp: &Timestamp) -> bool {
        // Note: This is a fix for Wormhole on mainnet.  The initial guardian set was never expired
        // so we block it here.
        if self.index == 0 && self.creation_time == 1628099186 {
            false
        } else {
            self.expiration_time == 0 || self.expiration_time >= *timestamp
        }
    }
```
