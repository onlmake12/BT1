### Title
`undelegate` in Integrity Pool Does Not Advance Delegation Record Before Modifying Positions, Causing Reward Loss - (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`, `governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

The `undelegate` instruction in the Pyth OIS (Oracle Integrity Staking) integrity pool program modifies `stakeAccountPositions` without first calling `advance_delegation_record` to settle pending rewards. Because `advance_delegation_record` reads the current `stakeAccountPositions` to compute historical rewards, a user who undelegates before claiming will have their pending rewards calculated against a reduced (or zeroed) position, causing permanent loss of accrued OIS rewards.

---

### Finding Description

The OIS reward model works as follows:

- `advance` (called at epoch boundaries) records per-epoch pool data into `pool_data.events`.
- `advance_delegation_record` iterates from `delegation_record.last_epoch` to the current epoch, reads the user's current `stakeAccountPositions`, and distributes rewards proportional to the user's stake at each epoch.
- `undelegate` modifies `stakeAccountPositions` (it is listed as `writable` in the instruction's account list) to reduce or remove the user's delegated position.

The critical flaw: `undelegate` does **not** include `delegation_record` in its accounts and does **not** invoke `advance_delegation_record` as a prerequisite. [1](#0-0) 

The `undelegate` instruction's account list contains `stakeAccountPositions` (writable) but no `delegation_record`, confirming no reward settlement occurs. [2](#0-1) 

`advance_delegation_record` takes `stakeAccountPositions` as a **read-only** input to compute rewards. If positions have already been removed or reduced by `undelegate`, the reward calculation uses the post-undelegate state, not the historical staked amounts.

The SDK's `getAdvanceDelegationRecordInstructions` compounds this: it filters out any publisher for which the user has **no current positions** in `stakeAccountPositions`. If a user fully undelegates from a publisher, the SDK will not even generate an `advance_delegation_record` instruction for that publisher, making it impossible to claim rewards through the normal flow. [3](#0-2) 

The `unstakeFromPublisher` function sends only `undelegate` instructions with no prior `advanceDelegationRecord` call: [4](#0-3) 

---

### Impact Explanation

A staking user who calls `undelegate` (or `unstakeFromAllPublishers`) before calling `advanceDelegationRecord` will permanently lose all pending OIS rewards accrued since `delegation_record.last_epoch`. The rewards are not recoverable because:

1. The on-chain `advance_delegation_record` reads current positions to compute historical rewards.
2. The SDK filters out publishers with no remaining positions, preventing the claim instruction from being generated at all.
3. There is no separate mechanism to recover rewards for a publisher from which the user has fully undelegated.

This is a direct loss of user funds (PYTH token rewards) with no recovery path.

---

### Likelihood Explanation

This is highly likely to occur in practice:

- The standard user flow for "unstake and withdraw" naturally involves calling `undelegate` first, then later claiming rewards — the UI separates these actions.
- The SDK's `unstakeFromPublisher` and `unstakeFromAllPublishers` functions do not internally call `advanceDelegationRecord` first.
- Any user who unstakes from a publisher without first explicitly claiming rewards (a separate, non-obvious step) will silently lose their pending rewards.
- The reward rate `y` is currently 0 per OP-PIP-103, but the mechanism remains active and `y` can be set non-zero by governance at any time, making this a latent high-severity issue. [5](#0-4) 

---

### Recommendation

1. **On-chain**: The `undelegate` instruction should require `delegation_record` as a writable account and invoke the reward-settlement logic (equivalent to `advance_delegation_record`) atomically before modifying positions.
2. **SDK**: `unstakeFromPublisher` and `unstakeFromAllPublishers` should prepend `advanceDelegationRecord` instructions for the affected publisher(s) before any `undelegate` instructions in the same transaction. [6](#0-5) 

---

### Proof of Concept

1. User delegates 1,000 PYTH to publisher P. `delegation_record.last_epoch = E`.
2. Several epochs pass. `pool_data.events` records rewards for epochs E+1 through E+5. User's unclaimed rewards = R PYTH.
3. User calls `undelegate(position_index, 1000)` — all positions for publisher P are removed from `stakeAccountPositions`. No `advance_delegation_record` is called.
4. User (or SDK) attempts to call `advance_delegation_record` for publisher P. The SDK's `getAdvanceDelegationRecordInstructions` finds no positions for publisher P (`lowestEpoch = undefined`) and filters out the instruction entirely.
5. User receives 0 rewards. R PYTH is permanently lost. [7](#0-6)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L301-346)
```json
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "publisher_stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "name": "delegation_record",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  100, 101, 108, 101, 103, 97, 116, 105, 111, 110, 95, 114, 101,
                  99, 111, 114, 100
                ]
              },
              {
                "kind": "account",
                "path": "publisher"
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
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [],
      "discriminator": [155, 43, 226, 175, 227, 115, 33, 88],
      "name": "advance_delegation_record",
      "returns": "u64"
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L991-1081)
```json
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "config_account",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
            },
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "stake_account_positions",
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "stake_account_metadata",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
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

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/mathematical-representation.mdx (L9-11)
```text
<Callout type="info" title="OP-PIP-103">
  Per [OP-PIP-103](https://proposals.pyth.network/proposals/103), the reward rate $y$ is currently set to **0**. The staking and slashing mechanisms remain active.
</Callout>
```
