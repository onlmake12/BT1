### Title
Pending OIS Rewards Permanently Lost When `undelegate` Is Called Without Prior `advance_delegation_record` - (File: `governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

The `undelegate` instruction in the `integrity_pool` program does not include `delegation_record` or `pool_reward_custody` in its account list, meaning it never calls `advance_delegation_record` internally. The SDK's `unstakeFromPublisher` (and `unstakeFromAllPublishers`) also does not call `advance_delegation_record` before submitting `undelegate` instructions. Any OIS rewards earned since the last `advance_delegation_record` call are permanently lost when a user undelegates.

---

### Finding Description

The `advance_delegation_record` instruction is the sole mechanism by which a delegator's pending OIS rewards are calculated and transferred from `pool_reward_custody` to `stake_account_custody`. It uses the `DelegationRecord.last_epoch` field to determine the range of epochs for which rewards are owed.

The `undelegate` instruction's account list (from the IDL) contains:

- `owner`, `pool_data`, `pool_config`, `publisher`, `config_account`, `stake_account_positions`, `stake_account_metadata`, `stake_account_custody`, `staking_program`, `system_program`

Critically absent: `delegation_record` and `pool_reward_custody`. The on-chain program therefore never advances the delegation record during undelegation. [1](#0-0) 

After `undelegate` removes or reduces the position in `stake_account_positions`, the SDK's `getAdvanceDelegationRecordInstructions` filters publishers by checking whether any positions with that publisher still exist in the stake account: [2](#0-1) 

Once the position is fully removed, the publisher is excluded from future `advance_delegation_record` calls, making the unclaimed rewards permanently inaccessible.

The SDK's `unstakeFromPublisher` submits only `undelegate` instructions with no preceding `advance_delegation_record`: [3](#0-2) 

The same issue exists in `unstakeFromAllPublishers`: [4](#0-3) 

---

### Impact Explanation

Any staking user who undelegates from a publisher pool without first manually calling `advance_delegation_record` (or `claim`) loses all OIS rewards earned since their last `advance_delegation_record` call. Because the SDK's standard unstake flow does not call `advance_delegation_record` first, this affects all users who use the standard UI or SDK to unstake. The `DelegationRecord` PDA persists on-chain but the position data needed to compute rewards is gone, so the rewards can never be recovered.

---

### Likelihood Explanation

High. Every user who unstakes via the standard SDK flow (`unstakeIntegrityStaking`, `cancelWarmupIntegrityStaking`, `unstakeAllIntegrityStaking`) is affected. No special attacker is needed — any ordinary staking user triggers this by performing a normal unstake operation. The `claim` step is a separate, optional action that users are not forced to take before undelegating. [5](#0-4) 

---

### Recommendation

In `unstakeFromPublisher` and `unstakeFromAllPublishers`, prepend `advance_delegation_record` instructions for the affected publisher(s) before the `undelegate` instructions, mirroring the pattern used in `advanceDelegationRecord`:

```typescript
const { advanceDelegationRecordInstructions } =
  await this.getAdvanceDelegationRecordInstructions(stakeAccountPositions);
// then send [...advanceDelegationRecordInstructions, ...undelegateInstructions]
```

Additionally, the on-chain `undelegate` instruction should ideally require the `delegation_record` account and advance it atomically, preventing reward loss even when called directly outside the SDK.

---

### Proof of Concept

1. User stakes to publisher P at epoch E via `delegate`.
2. Several epochs pass; `advance_delegation_record` is never called (rewards accumulate).
3. User calls `unstakeIntegrityStaking` → SDK calls `undelegate` directly.
4. The position for publisher P is removed from `stake_account_positions`.
5. User calls `claim` → SDK calls `getAdvanceDelegationRecordInstructions` → publisher P is filtered out because no positions remain → zero `advance_delegation_record` instructions are generated.
6. Rewards for epochs E through current are permanently lost. [6](#0-5)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1021-1081)
```json
            },
            "seeds": [
              {
                "kind": "const",
                "value": [
                  115, 116, 97, 107, 101, 95, 109, 101, 116, 97, 100, 97, 116,
                  97
                ]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "stake_account_custody",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
            },
            "seeds": [
              {
                "kind": "const",
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

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L373-401)
```typescript
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

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L684-735)
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
```

**File:** apps/staking/src/api.ts (L385-407)
```typescript
export const unstakeIntegrityStaking = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
  publisherKey: PublicKey,
  amount: bigint,
): Promise<void> => {
  await client.unstakeFromPublisher(
    stakeAccount,
    publisherKey,
    PositionState.LOCKED,
    amount,
  );
};

export const unstakeAllIntegrityStaking = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
): Promise<void> => {
  await client.unstakeFromAllPublishers(stakeAccount, [
    PositionState.LOCKED,
    PositionState.LOCKING,
  ]);
};
```
