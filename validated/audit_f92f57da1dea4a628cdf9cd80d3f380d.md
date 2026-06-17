### Title
OIS Staking Rewards Permanently Unclaimable After Full Unstake from Publisher - (`governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

`PythStakingClient.getAdvanceDelegationRecordInstructions()` filters the publisher list to only those where the caller currently holds active positions. If a user unstakes all positions from a publisher before calling `claim`, no `advance_delegation_record` instruction is generated for that publisher, and any accumulated but unclaimed OIS rewards are permanently inaccessible through the SDK.

---

### Finding Description

In `governance/pyth_staking_sdk/src/pyth-staking-client.ts`, `getAdvanceDelegationRecordInstructions` (the sole backend for both `advanceDelegationRecord` / `claim`) builds its publisher list as follows:

```typescript
// lines 693-713
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
  .filter(({ lowestEpoch }) => lowestEpoch !== undefined); // ← only publishers with live positions
``` [1](#0-0) 

A publisher is included only when `lowestEpoch !== undefined`, which requires at least one current position with that publisher. If the user has called `unstakeFromPublisher` and removed every position, `positionsWithPublisher` is empty, `lowestEpoch` stays `undefined`, and the publisher is silently dropped from the list. [2](#0-1) 

Consequently, `advanceDelegationRecordInstructions` is an empty array for that publisher, and `advanceDelegationRecord` / `claim` submits a transaction with no reward-claiming instructions for it:

```typescript
// lines 779-792
public async advanceDelegationRecord(stakeAccountPositions: PublicKey) {
  const instructions = await this.getAdvanceDelegationRecordInstructions(
    stakeAccountPositions,
  );
  return sendTransaction(
    [
      ...instructions.advanceDelegationRecordInstructions,
      ...instructions.mergePositionsInstruction,
    ],
    ...
  );
}
``` [3](#0-2) 

The on-chain `advance_delegation_record` instruction is keyed on `(publisher, stake_account_positions)` and does not require live positions to exist — it only needs a `delegation_record` PDA with unclaimed epochs. The SDK-side filter is therefore overly restrictive and silently drops valid claim instructions. [4](#0-3) 

The `claim` entry point in the staking app calls directly into this path with no fallback:

```typescript
export const claim = async (client, stakeAccount) => {
  await client.advanceDelegationRecord(stakeAccount);
};
``` [5](#0-4) 

`unstakeFromPublisher` does not call `advanceDelegationRecord` before removing positions, so rewards are not automatically settled on unstake: [6](#0-5) 

---

### Impact Explanation

Any OIS staker who fully unstakes from a publisher without first explicitly claiming rewards loses those rewards permanently through the standard SDK/UI interface. The `delegation_record` PDA retains the unclaimed epoch data on-chain, but the SDK never generates the instruction to process it. The rewards remain locked in `pool_reward_custody` and are never transferred to the user's `stake_account_custody`. [7](#0-6) 

---

### Likelihood Explanation

The claim step is a separate, explicit UI action. The natural user flow — stake → earn → unstake → claim — triggers this bug because unstaking removes positions before the claim is submitted. Any user who follows this order loses rewards. The staking UI does not warn users to claim before unstaking. [8](#0-7) 

---

### Recommendation

Change the publisher filter to include any publisher for which a non-null `delegation_record` exists with `lastEpoch < currentEpoch`, regardless of whether the user still holds positions. Concretely:

1. Fetch `delegationRecords` for **all** publishers (not just those with live positions).
2. Include a publisher in `filteredPublishers` if its `delegationRecord` exists and `lastEpoch < currentEpoch`.
3. Alternatively, enforce that `advanceDelegationRecord` is called atomically before `undelegate` inside `unstakeFromPublisher`, so rewards are always settled before positions are removed. [9](#0-8) 

---

### Proof of Concept

1. User stakes 1000 PYTH with publisher `P` at epoch `E`.
2. Epochs `E`, `E+1`, `E+2` pass; rewards accumulate in the `delegation_record` for `(P, stakeAccount)`.
3. User calls `unstakeFromPublisher(stakeAccount, P, LOCKED, 1000)` — all positions removed.
4. User calls `claim(client, stakeAccount)` → `advanceDelegationRecord(stakeAccount)`.
5. Inside `getAdvanceDelegationRecordInstructions`: `positionsWithPublisher` for `P` is `[]`, `lowestEpoch` stays `undefined`, publisher `P` is excluded at line 713.
6. `advanceDelegationRecordInstructions` is `[]`; transaction is submitted with no reward instructions.
7. `getClaimableRewards` also returns `0n` for the same reason (it calls the same function), so the UI shows no claimable rewards — the user has no indication anything is wrong.
8. Rewards remain in `pool_reward_custody` indefinitely; the user's `delegation_record` is never advanced. [10](#0-9)

### Citations

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L346-401)
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
  }
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L684-757)
```typescript
  async getAdvanceDelegationRecordInstructions(
    stakeAccountPositions: PublicKey,
    payer?: PublicKey,
  ) {
    const poolData = await this.getPoolDataAccount();
    const stakeAccountPositionsData = await this.getStakeAccountPositions(
      stakeAccountPositions,
    );
    const allPublishers = extractPublisherData(poolData);
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

    const delegationRecords = await Promise.all(
      publishers.map(({ pubkey }) =>
        this.getDelegationRecord(stakeAccountPositions, pubkey),
      ),
    );

    let lowestEpoch: bigint | undefined;
    for (const [index, publisher] of publishers.entries()) {
      const maximum = bigintMax(
        publisher.lowestEpoch,
        delegationRecords[index]?.lastEpoch,
      );
      lowestEpoch = bigintMin(lowestEpoch, maximum);
    }

    const currentEpoch = await getCurrentEpoch(this.connection);

    // Filter out delegationRecord that are up to date
    const filteredPublishers = publishers.filter((_, index) => {
      return !(delegationRecords[index]?.lastEpoch === currentEpoch);
    });

    // anchor does not calculate the correct pda for other programs
    // therefore we need to manually calculate the pdas
    const advanceDelegationRecordInstructions = await Promise.all(
      filteredPublishers.map(({ pubkey, stakeAccount }) =>
        this.integrityPoolProgram.methods
          .advanceDelegationRecord()
          .accountsPartial({
            payer: payer ?? this.wallet.publicKey,
            publisher: pubkey,
            publisherStakeAccountCustody: stakeAccount
              ? getStakeAccountCustodyAddress(stakeAccount)
              : null, // eslint-disable-line unicorn/no-null
            publisherStakeAccountPositions: stakeAccount,
            stakeAccountCustody: getStakeAccountCustodyAddress(
              stakeAccountPositions,
            ),
            stakeAccountPositions,
          })
          .instruction(),
      ),
    );
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

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L794-825)
```typescript
  public async getClaimableRewards(
    stakeAccountPositions: PublicKey,
    simulationPayer?: PublicKey,
  ) {
    const instructions = await this.getAdvanceDelegationRecordInstructions(
      stakeAccountPositions,
      simulationPayer,
    );

    let totalRewards = 0n;

    for (const instruction of instructions.advanceDelegationRecordInstructions) {
      const tx = new Transaction().add(instruction);
      tx.feePayer = simulationPayer ?? this.wallet.publicKey;
      // eslint-disable-next-line @typescript-eslint/no-deprecated
      const res = await this.connection.simulateTransaction(tx);
      const val = res.value.returnData?.data[0];
      if (val === undefined) {
        continue;
      }
      const buffer = Buffer.from(val, "base64").reverse();
      totalRewards += BigInt("0x" + buffer.toString("hex"));
    }

    return {
      expiry:
        instructions.lowestEpoch === undefined
          ? undefined
          : epochToDate(instructions.lowestEpoch + 53n),
      totalRewards,
    };
  }
```

**File:** apps/staking/src/api.ts (L315-328)
```typescript
export const withdraw = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
  amount: bigint,
): Promise<void> => {
  await client.withdrawTokensFromStakeAccountCustody(stakeAccount, amount);
};

export const claim = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
): Promise<void> => {
  await client.advanceDelegationRecord(stakeAccount);
};
```
