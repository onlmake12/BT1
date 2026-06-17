### Title
Permissionless `execute_posted_vaa` with Non-Strict Sequence Check Allows Governance VAA Skipping — (File: `governance/remote_executor/programs/remote-executor/src/lib.rs`)

---

### Summary

The `execute_posted_vaa` instruction in Pyth's remote executor program has no caller access control — any user can invoke it. Combined with a non-strict sequence check (`posted_vaa.sequence > claim_record.sequence`), an attacker can execute a higher-sequence governance VAA before the authorized relayer processes lower-sequence ones, permanently blocking those earlier VAAs from ever executing. There is no cancel mechanism: once a VAA is signed by Wormhole guardians, neither Pyth governance nor the multisig can prevent its execution or revoke it.

---

### Finding Description

The `execute_posted_vaa` function in the remote executor is the Pyth analog to Compound's `executeProposal`. It is declared `pub fn execute_posted_vaa(ctx: Context<ExecutePostedVaa>)` with no authority check on the caller — the only signer required is `payer`, which is just the transaction fee payer and can be any keypair. [1](#0-0) 

The account constraint on `posted_vaa` enforces only:

```
posted_vaa.sequence > claim_record.sequence
``` [2](#0-1) 

This is a **non-strict** (non-consecutive) check. It does not require `posted_vaa.sequence == claim_record.sequence + 1`. The `ClaimRecord` stores only the last executed sequence: [3](#0-2) 

**Attack path:**

1. Governance multisig on Pythnet signs and posts VAA #N (e.g., set data sources) and VAA #N+K (e.g., upgrade contract) to Wormhole.
2. Both VAAs are publicly observable on the Wormhole network.
3. Attacker calls Wormhole's permissionless `postVaa` to post VAA #N+K to Solana.
4. Attacker calls `execute_posted_vaa` with VAA #N+K. `claim_record.sequence` is now N+K.
5. VAA #N (and all VAAs with sequence ≤ N+K) can **never** be executed — the `NonIncreasingSequence` constraint will permanently reject them.

The same issue exists in the EVM `Executor.sol`, where `execute()` is `public payable` with no caller restriction, and the sequence check is `vm.sequence <= lastExecutedSequence` (also non-strict/non-consecutive): [4](#0-3) [5](#0-4) 

The `crank_pythnet_relayer` processes VAAs sequentially and, when `SKIP_FAILED_REMOTE_INSTRUCTIONS` is enabled, silently skips failed executions — meaning the skipped VAA is permanently lost with no alert: [6](#0-5) 

There is no cancel instruction in the remote executor program. The error codes confirm no such functionality exists: [7](#0-6) 

---

### Impact Explanation

An unprivileged attacker can permanently prevent specific governance VAAs from executing by front-running the relayer with a higher-sequence VAA. Skipped VAAs could include: fee parameter changes, data source updates, contract upgrade authorizations, or governance authority transfers. The governance multisig has no on-chain mechanism to cancel or re-sequence a VAA once it has been signed by Wormhole guardians. The only recovery path would be to pass a new governance proposal — but if the skipped VAA was itself a critical security patch, the window of exposure is extended.

---

### Likelihood Explanation

Wormhole VAAs are publicly observable. Posting a VAA to Solana via `postVaa` is permissionless and costs only transaction fees. The attacker does not need any privileged key, governance role, or oracle access. The attack requires only: (1) monitoring Wormhole for governance VAAs, and (2) submitting a Solana transaction before the `crank_pythnet_relayer`. This is a realistic front-running scenario on a public network.

---

### Recommendation

1. **Enforce consecutive sequence**: Change the constraint from `posted_vaa.sequence > claim_record.sequence` to `posted_vaa.sequence == claim_record.sequence + 1` to prevent out-of-order execution.
2. **Add caller access control**: Restrict `execute_posted_vaa` to a whitelisted relayer address or the governance multisig authority, analogous to how `BaseBridgeReceiver` should have restricted `executeProposal`.
3. **EVM Executor**: Apply the same consecutive-sequence enforcement and consider adding an `onlyOwner`/`onlyGovernance` modifier to `execute()`.

---

### Proof of Concept

The existing test suite inadvertently demonstrates the gap — `vaa_account_transfer2` has sequence 3 and is executed after sequence 1, skipping sequence 2: [8](#0-7) 

An attacker replicates this by substituting a real governance VAA with a higher sequence number, posting it via Wormhole's public `postVaa`, and calling `execute_posted_vaa` — permanently blocking all lower-sequence governance VAAs.

### Citations

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L29-55)
```rust
    pub fn execute_posted_vaa(ctx: Context<ExecutePostedVaa>) -> Result<()> {
        let posted_vaa = &ctx.accounts.posted_vaa;
        let claim_record = &mut ctx.accounts.claim_record;
        claim_record.sequence = posted_vaa.sequence;

        let payload = ExecutorPayload::try_from_slice(&posted_vaa.payload)?;
        payload.check_header()?;

        let (_, bump) = Pubkey::find_program_address(
            &[EXECUTOR_KEY_SEED.as_bytes(), &posted_vaa.emitter_address],
            &id(),
        );

        for instruction in payload.instructions.iter().map(Instruction::from) {
            // TO DO: We currently pass `remaining_accounts` down to the CPIs, is there a more efficient way to do it?
            invoke_signed(
                &instruction,
                ctx.remaining_accounts,
                &[&[
                    EXECUTOR_KEY_SEED.as_bytes(),
                    &posted_vaa.emitter_address,
                    &[bump],
                ]],
            )?;
        }
        Ok(())
    }
```

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L65-69)
```rust
    #[account(constraint = Chain::from(posted_vaa.emitter_chain) == Solana @ ExecutorError::EmitterChainNotSolana, constraint = posted_vaa.sequence > claim_record.sequence @ExecutorError::NonIncreasingSequence, constraint = (&posted_vaa.magic == b"vaa" || &posted_vaa.magic == b"msg" || &posted_vaa.magic == b"msu") @ExecutorError::PostedVaaHeaderWrongMagicNumber )]
    pub posted_vaa: Account<'info, AnchorVaa>,
    /// The reason claim_record has different seeds than executor_key is that executor key might need to pay in the CPI, so we want it to be a native wallet
    #[account(init_if_needed, space = 8 + ClaimRecord::LEN, payer=payer, seeds = [CLAIM_RECORD_SEED.as_bytes(), &posted_vaa.emitter_address], bump)]
    pub claim_record: Account<'info, ClaimRecord>,
```

**File:** governance/remote_executor/programs/remote-executor/src/state/claim_record.rs (L9-11)
```rust
pub struct ClaimRecord {
    pub sequence: u64,
}
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L66-68)
```text
    function execute(
        bytes memory encodedVm
    ) public payable returns (bytes memory response) {
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L131-134)
```text
        if (vm.sequence <= lastExecutedSequence)
            revert ExecutorErrors.MessageOutOfOrder();

        lastExecutedSequence = vm.sequence;
```

**File:** governance/xc_admin/packages/crank_pythnet_relayer/src/index.ts (L207-211)
```typescript
        } catch (e) {
          if (SKIP_FAILED_REMOTE_INSTRUCTIONS) {
            console.error(e);
          } else throw e;
        }
```

**File:** governance/remote_executor/programs/remote-executor/src/error.rs (L1-12)
```rust
use anchor_lang::prelude::*;

#[error_code]
pub enum ExecutorError {
    EmitterChainNotSolana,
    NonIncreasingSequence,
    GovernanceHeaderInvalidMagicNumber,
    GovernanceHeaderInvalidModule,
    GovernanceHeaderInvalidAction,
    GovernanceHeaderInvalidReceiverChain,
    PostedVaaHeaderWrongMagicNumber,
}
```

**File:** governance/remote_executor/programs/remote-executor/src/tests/test_basic_instructions.rs (L99-134)
```rust
    // Execute one transfer
    sim.execute_posted_vaa(&vaa_account_transfer2, &vec![], ExecutorAttack::None)
        .await
        .unwrap();

    // Check state post call
    let post_account4 = sim.get_account(receiver4.pubkey()).await.unwrap();
    assert_eq!(post_account4.lamports, Rent::default().minimum_balance(0));
    assert_eq!(post_account4.data.len(), 0);
    assert_eq!(post_account4.owner, system_program::id());

    let claim_record_data = sim.get_claim_record(emitter).await;
    assert_eq!(claim_record_data.sequence, 3);

    // Replay attack
    assert_eq!(
        sim.execute_posted_vaa(&vaa_account_transfer2, &vec![], ExecutorAttack::None)
            .await
            .unwrap_err()
            .unwrap(),
        ExecutorError::NonIncreasingSequence.into()
    );

    let claim_record_data = sim.get_claim_record(emitter).await;
    assert_eq!(claim_record_data.sequence, 3);

    // Using a governance message with a lower sequence number
    assert_eq!(
        sim.execute_posted_vaa(&vaa_account_transfer1, &vec![], ExecutorAttack::None)
            .await
            .unwrap_err()
            .unwrap(),
        ExecutorError::NonIncreasingSequence.into()
    );
    let claim_record_data = sim.get_claim_record(emitter).await;
    assert_eq!(claim_record_data.sequence, 3);
```
