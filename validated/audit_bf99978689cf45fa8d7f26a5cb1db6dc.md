### Title
Staking Reward Loss on Undelegate — Missing `advance_delegation_record` Before Position Closure (`governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

The `unstakeFromPublisher` and `unstakeFromAllPublishers` methods in `PythStakingClient` call the on-chain `undelegate` instruction without first calling `advance_delegation_record`. This is the direct structural analog to the PoolTogether ERC777 bug: a state-changing operation that reduces a user's staked position does not first settle the user's pending epoch rewards, potentially causing permanent reward loss.

---

### Finding Description

In the OIS (Oracle Integrity Staking) system, rewards are not automatically credited to a user's stake account custody on each epoch boundary. Instead, they accumulate in the pool's reward custody and are only transferred to the user's stake account custody when `advance_delegation_record` is explicitly called. The `DelegationRecord` PDA tracks `last_epoch` — the last epoch for which rewards were settled for a given (publisher, staker) pair. [1](#0-0) 

`unstakeFromPublisher` constructs and sends `undelegate` instructions directly, with no call to `advanceDelegationRecord` or `getAdvanceDelegationRecordInstructions` anywhere in the method body. The same is true for `unstakeFromAllPublishers`: [2](#0-1) 

By contrast, the `claim` function in `apps/staking/src/api.ts` correctly calls `advanceDelegationRecord` as its sole action: [3](#0-2) 

The `advance_delegation_record` on-chain instruction requires `stake_account_positions` as an input to compute rewards. Once a position is closed via `undelegate`, the position data no longer reflects the historical delegation, and a subsequent call to `advance_delegation_record` for that publisher may compute zero rewards for the now-closed position — permanently forfeiting the unsettled epochs. [4](#0-3) 

The `DelegationRecord` struct confirms that reward settlement is epoch-gated via `last_epoch`: [5](#0-4) 

The `undelegate` instruction's account list (from the IDL) does **not** include `delegation_record` or `pool_reward_custody`, confirming it performs no reward settlement: [6](#0-5) 

---

### Impact Explanation

A staking user who calls `unstakeFromPublisher` (or `unstakeFromAllPublishers`) without first calling `advanceDelegationRecord` forfeits all OIS rewards earned since their `DelegationRecord.last_epoch`. Because the SDK does not enforce or prepend the advance step, any user relying on the SDK's default flow loses pending rewards silently. The reward expiry window is 53 epochs (~1 year), so users with long-standing positions and multiple publishers are most exposed.

---

### Likelihood Explanation

The default SDK path for undelegating (`unstakeFromPublisher`, `unstakeFromAllPublishers`) never calls `advanceDelegationRecord`. Any user who undelegates through the standard SDK interface — which is the primary integration surface — will trigger this path. The UI in `apps/staking` calls these SDK methods directly. This is not a corner case; it is the default behavior for every undelegate operation. [7](#0-6) 

---

### Recommendation

Prepend `advance_delegation_record` instructions for the relevant publisher before each `undelegate` instruction in both `unstakeFromPublisher` and `unstakeFromAllPublishers`, mirroring the pattern already used in `advanceDelegationRecord`. Alternatively, enforce at the on-chain program level that `advance_delegation_record` must be current (i.e., `delegation_record.last_epoch == current_epoch`) before `undelegate` is permitted, similar to how the error `OutdatedPublisherAccounting` (code 6003) is used elsewhere. [8](#0-7) 

---

### Proof of Concept

1. User has an active delegation to publisher P, with `delegation_record.last_epoch = E-3` (three epochs of unsettled rewards).
2. User calls `unstakeFromPublisher(stakeAccountPositions, P, LOCKED, amount)`.
3. The SDK sends only `undelegate(index, amount)` — no `advance_delegation_record` is included.
4. The position for publisher P is closed on-chain.
5. User later calls `advanceDelegationRecord(stakeAccountPositions)` to claim rewards.
6. `getAdvanceDelegationRecordInstructions` filters publishers by checking `positionsWithPublisher` — since the position for P is now closed (amount = 0 or removed), publisher P is excluded from the advance instructions.
7. Rewards for epochs E-3, E-2, E-1 are never transferred to the user's stake account custody and remain stranded in the pool reward custody. [9](#0-8)

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

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L403-441)
```typescript
  public async unstakeFromAllPublishers(
    stakeAccountPositions: PublicKey,
    positionStates: (PositionState.LOCKED | PositionState.LOCKING)[],
  ) {
    const [stakeAccountPositionsData, currentEpoch] = await Promise.all([
      this.getStakeAccountPositions(stakeAccountPositions),
      getCurrentEpoch(this.connection),
    ]);

    const instructions = await Promise.all(
      stakeAccountPositionsData.data.positions
        .map((position, index) => {
          const publisher =
            position.targetWithParameters.integrityPool?.publisher;
          return publisher === undefined
            ? undefined
            : { index, position, publisher };
        })
        // By separating this filter from the next, typescript can narrow the
        // type and automatically infer that there will be no `undefined` values
        // in the array after this line.  If we combine those filters,
        // typescript won't narrow properly.
        .filter((positionInfo) => positionInfo !== undefined)
        .filter(({ position }) =>
          (positionStates as PositionState[]).includes(
            getPositionState(position, currentEpoch),
          ),
        )
        .reverse()
        .map(({ position, index, publisher }) =>
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(position.amount))
            .accounts({ publisher, stakeAccountPositions })
            .instruction(),
        ),
    );

    return sendTransaction(instructions, this.connection, this.wallet);
  }
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L684-713)
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

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L50-53)
```json
    {
      "code": 6003,
      "name": "OutdatedPublisherAccounting"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1051-1081)
```json
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          }
        },
        {
          "address": "pytS9TjG1qyAZypk7n8rw8gfW9sUaqqYyMhJQ4E7JCQ",
          "name": "staking_program"
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [
        {
          "name": "position_index",
          "type": "u8"
        },
        {
          "name": "amount",
          "type": "u64"
        }
      ],
      "discriminator": [131, 148, 180, 198, 91, 104, 42, 238],
      "name": "undelegate"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1195-1208)
```json
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
