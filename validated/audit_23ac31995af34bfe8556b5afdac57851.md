### Title
Unclaimed OIS Rewards Permanently Inaccessible After Full Undelegation - (`governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

### Summary

The `getAdvanceDelegationRecordInstructions` function in the Pyth staking SDK filters publishers exclusively by the presence of active on-chain positions. When a user fully undelegates from a publisher without first claiming rewards, the SDK will never generate an `advance_delegation_record` instruction for that publisher, making accrued but unclaimed OIS rewards permanently inaccessible through the standard interface.

### Finding Description

In `getAdvanceDelegationRecordInstructions`, the SDK builds the list of publishers to claim from by scanning `stakeAccountPositions` for active positions:

```typescript
const publishers = allPublishers
  .map((publisher) => {
    const positionsWithPublisher =
      stakeAccountPositionsData.data.positions.filter(
        ({ targetWithParameters }) =>
          targetWithParameters.integrityPool?.publisher.equals(publisher.pubkey),
      );

    let lowestEpoch;
    for (const position of positionsWithPublisher) {
      lowestEpoch = bigintMin(position.activationEpoch, lowestEpoch);
    }

    return { ...publisher, lowestEpoch };
  })
  .filter(({ lowestEpoch }) => lowestEpoch !== undefined);  // ← drops publishers with no positions
```

If `positionsWithPublisher` is empty (all positions closed via `undelegate`), `lowestEpoch` remains `undefined` and the publisher is dropped from the list. No `advance_delegation_record` instruction is generated for that publisher.

The `claim` flow in `apps/staking/src/api.ts` calls `client.advanceDelegationRecord(stakeAccount)`, which calls `getAdvanceDelegationRecordInstructions`, which calls `advanceDelegationRecord` only for the filtered set. Publishers with no remaining positions are silently skipped.

The on-chain `DelegationRecord` account (keyed by `[publisher, stakeAccountPositions]`) still holds `last_epoch < current_epoch` for such publishers, meaning unclaimed rewards exist on-chain but the SDK never generates the instruction to collect them.

The `unstakeFromPublisher` function calls `undelegate` directly without any prior `advance_delegation_record` call, making it trivially easy for a user to reach this state:

```typescript
instructionPromises.push(
  this.integrityPoolProgram.methods
    .undelegate(index, convertBigIntToBN(position.amount))
    .accounts({ publisher, stakeAccountPositions })
    .instruction(),
);
```

### Impact Explanation

Any staking user who calls `unstakeFromPublisher` (or the underlying `undelegate` instruction) without first calling `claim` will permanently lose all OIS rewards accrued since their last `advance_delegation_record` call. The rewards remain locked in the `pool_reward_custody` account with no SDK-accessible path to retrieve them. The `getClaimableRewards` function also uses the same filtered instruction set, so the UI will display `0` claimable rewards for the affected user, giving no indication that rewards exist.

### Likelihood Explanation

This is a normal user flow: a user decides to exit their OIS position and calls unstake. The UI does not enforce or prompt a claim-before-unstake ordering. Any user who unstakes in a single step (without a separate prior claim transaction) will silently lose rewards. The likelihood is high given the absence of any guard or warning in the SDK or UI.

### Recommendation

`getAdvanceDelegationRecordInstructions` should also include publishers for which a `DelegationRecord` exists with `last_epoch < current_epoch`, regardless of whether active positions remain. The fix is to fetch all `DelegationRecord` accounts associated with the stake account (not just those with active positions) and include them in the instruction set when they are not yet up to date:

```typescript
// Also include publishers where DelegationRecord exists but no active positions remain
const publishersWithRecord = allPublishers.filter(async ({ pubkey }) => {
  const record = await this.getDelegationRecord(stakeAccountPositions, pubkey);
  return record !== null && record.lastEpoch < currentEpoch;
});
```

### Proof of Concept

1. User Alice delegates 1000 PYTH to publisher P at epoch 10. Rewards accrue over epochs 10–20.
2. At epoch 20, Alice calls `unstakeFromPublisher` (which calls `undelegate`) without calling `claim` first.
3. Alice's `stakeAccountPositions` now has no positions targeting publisher P.
4. Alice calls `claim` → `advanceDelegationRecord` → `getAdvanceDelegationRecordInstructions`.
5. The function maps over all publishers; for publisher P, `positionsWithPublisher` is empty, so `lowestEpoch = undefined`.
6. Publisher P is filtered out at line 713: `.filter(({ lowestEpoch }) => lowestEpoch !== undefined)`.
7. No `advance_delegation_record` instruction is generated for P.
8. Alice's `DelegationRecord` for P still has `last_epoch = 10`, but no instruction is ever submitted to advance it.
9. Alice's rewards for epochs 10–20 are permanently inaccessible through the SDK.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L346-400)
```typescript
  public async unstakeFromPublisher(
    stakeAccountPositions: PublicKey,
    publisher: PublicKey,
    positionState: PositionState.LOCKED | PositionState.LOCKING,
    amount: bigint,
  ) {
    const stakeAccountPositionsData = await this.getStakeAccountPositions(
      stakeAccountPositions,
    );
    const currentEpoch = await getCurrentEpoch(this.connection);

    let remainingAmount = amount;
    const instructionPromises: Promise<TransactionInstruction>[] = [];

    const eligiblePositions = stakeAccountPositionsData.data.positions
      .map((p, i) => ({ index: i, position: p }))
      .reverse()
      .filter(
        ({ position }) =>
          position.targetWithParameters.integrityPool?.publisher !==
            undefined &&
          position.targetWithParameters.integrityPool.publisher.equals(
            publisher,
          ) &&
          positionState === getPositionState(position, currentEpoch),
      );

    for (const { position, index } of eligiblePositions) {
      if (position.amount < remainingAmount) {
        instructionPromises.push(
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(position.amount))
            .accounts({
              publisher,
              stakeAccountPositions,
            })
            .instruction(),
        );
        remainingAmount -= position.amount;
      } else {
        instructionPromises.push(
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(remainingAmount))
            .accounts({
              publisher,
              stakeAccountPositions,
            })
            .instruction(),
        );
        break;
      }
    }

    const instructions = await Promise.all(instructionPromises);
    return sendTransaction(instructions, this.connection, this.wallet);
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L693-713)
```typescript
    const publishers = allPublishers
      .map((publisher) => {
        const positionsWithPublisher =
          stakeAccountPositionsData.data.positions.filter(
            ({ targetWithParameters }) =>
              targetWithParameters.integrityPool?.publisher.equals(
                publisher.pubkey,
              ),
          );

        let lowestEpoch;
        for (const position of positionsWithPublisher) {
          lowestEpoch = bigintMin(position.activationEpoch, lowestEpoch);
        }

        return {
          ...publisher,
          lowestEpoch,
        };
      })
      .filter(({ lowestEpoch }) => lowestEpoch !== undefined);
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L779-792)
```typescript
  public async advanceDelegationRecord(stakeAccountPositions: PublicKey) {
    const instructions = await this.getAdvanceDelegationRecordInstructions(
      stakeAccountPositions,
    );

    return sendTransaction(
      [
        ...instructions.advanceDelegationRecordInstructions,
        ...instructions.mergePositionsInstruction,
      ],
      this.connection,
      this.wallet,
    );
  }
```

**File:** apps/staking/src/api.ts (L323-328)
```typescript
export const claim = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
): Promise<void> => {
  await client.advanceDelegationRecord(stakeAccount);
};
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1194-1208)
```json
    {
      "name": "DelegationRecord",
      "type": {
        "fields": [
          {
            "name": "last_epoch",
            "type": "u64"
          },
          {
            "name": "next_slash_event_index",
            "type": "u64"
          }
        ],
        "kind": "struct"
      }
```
