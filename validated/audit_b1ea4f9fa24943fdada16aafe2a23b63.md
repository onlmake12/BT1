### Title
Staking Users Forfeit Pending OIS Rewards When Undelegating Without Prior `advance_delegation_record` - (`governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

### Summary

The `unstakeFromPublisher`, `unstakeFromAllPublishers`, and `reassignPublisherStakeAccount` functions in `pyth-staking-client.ts` do not call `advance_delegation_record` before closing or migrating staking positions. After positions are closed, the SDK's own reward-instruction builder filters out publishers with no remaining positions, making pending rewards permanently unreachable through the standard SDK path.

### Finding Description

In the OIS (Oracle Integrity Staking) system, rewards are settled lazily via the `advance_delegation_record` on-chain instruction. This instruction must be called per-publisher to transfer accrued rewards into the user's custody account. The SDK exposes this as `advanceDelegationRecord` / `claim`.

The three affected flows are:

**1. `unstakeFromPublisher`** — sends only `undelegate` instructions with no prior reward settlement: [1](#0-0) 

**2. `unstakeFromAllPublishers`** — same pattern, bulk variant: [2](#0-1) 

**3. `reassignPublisherStakeAccount`** — calls `setPublisherStakeAccount` directly, no reward settlement: [3](#0-2) 

After any of these calls, the SDK's `getAdvanceDelegationRecordInstructions` filters publishers by scanning **current** positions in the stake account. If all positions for a publisher have been closed, `lowestEpoch` is `undefined` and that publisher is excluded from the instruction set: [4](#0-3) 

This means the standard `claim` path (`advanceDelegationRecord`) will silently skip the publisher whose position was just closed, and the pending rewards for that publisher are never transferred to the user's custody.

The on-chain `undelegate` instruction does not include `delegation_record` or `pool_reward_custody` as accounts, confirming it performs no reward settlement: [5](#0-4) 

The `advance_delegation_record` instruction, by contrast, does include `pool_reward_custody` and `stake_account_custody` as writable accounts for the actual token transfer: [6](#0-5) 

### Impact Explanation

Any staking user who calls `unstakeFromPublisher` or `unstakeFromAllPublishers` (or a publisher who calls `reassignPublisherStakeAccount`) without first calling `claim` will permanently forfeit all pending OIS rewards accrued since their last `advance_delegation_record`. The rewards remain in `pool_reward_custody` and are not redistributed — they are simply never claimed.

**Important caveat**: Per OP-PIP-103, the reward rate `y` is currently set to 0, so no rewards are actively accruing at this time: [7](#0-6) 

The vulnerability is latent and will become exploitable if/when `y` is set to a non-zero value.

### Likelihood Explanation

Any unprivileged staking user interacting through the standard SDK path is affected. The `unstakeAllIntegrityStaking` function exposed in the staking app UI is the most likely trigger — it is called when users in restricted regions must forcibly exit all OIS positions: [8](#0-7) 

Users in this flow have no UI prompt to claim rewards first, making inadvertent forfeiture likely.

### Recommendation

In `unstakeFromPublisher`, `unstakeFromAllPublishers`, and `reassignPublisherStakeAccount`, prepend `advance_delegation_record` instructions for all affected publishers before sending the `undelegate` / `setPublisherStakeAccount` instructions. Concretely, call `getAdvanceDelegationRecordInstructions` for the relevant publishers **before** closing positions and include those instructions at the head of the transaction batch.

Additionally, the `getAdvanceDelegationRecordInstructions` helper should be updated to accept an explicit publisher list rather than deriving it solely from current open positions, so that reward settlement remains possible even after positions are closed.

### Proof of Concept

1. User delegates 1000 PYTH to publisher P. Rewards accrue over several epochs (when `y > 0`).
2. User calls `unstakeIntegrityStaking` (→ `unstakeFromPublisher` → `undelegate`). No `advance_delegation_record` is sent.
3. User later calls `claim` (→ `advanceDelegationRecord` → `getAdvanceDelegationRecordInstructions`).
4. Inside `getAdvanceDelegationRecordInstructions`, the filter at line 713 excludes publisher P because no positions remain for P in the stake account.
5. No `advance_delegation_record` instruction is generated for P. The pending rewards are never transferred. The delegation record's `lastEpoch` remains stale but is never advanced. [9](#0-8) [10](#0-9)

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

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L846-856)
```typescript
  public async reassignPublisherStakeAccount(
    publisher: PublicKey,
    stakeAccountPositions: PublicKey,
    newStakeAccountPositions: PublicKey,
  ) {
    return this.setPublisherStakeAccount(
      publisher,
      stakeAccountPositions,
      newStakeAccountPositions,
    );
  }
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L136-260)
```typescript
    },
    {
      name: "advanceDelegationRecord";
      discriminator: [155, 43, 226, 175, 227, 115, 33, 88];
      accounts: [
        {
          name: "payer";
          writable: true;
          signer: true;
        },
        {
          name: "stakeAccountPositions";
        },
        {
          name: "poolData";
          writable: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "poolRewardCustody";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "account";
                path: "poolConfig";
              },
              {
                kind: "const";
                value: [
                  6,
                  221,
                  246,
                  225,
                  215,
                  101,
                  161,
                  147,
                  217,
                  203,
                  225,
                  70,
                  206,
                  235,
                  121,
                  172,
                  28,
                  180,
                  133,
                  237,
                  95,
                  91,
                  55,
                  145,
                  58,
                  140,
                  245,
                  133,
                  126,
                  255,
                  0,
                  169,
                ];
              },
              {
                kind: "account";
                path: "pool_config.pyth_token_mint";
                account: "poolConfig";
              },
            ];
            program: {
              kind: "const";
              value: [
                140,
                151,
                37,
                143,
                78,
                36,
                137,
                241,
                187,
                61,
                16,
                41,
                20,
                142,
                13,
                131,
                11,
                90,
                19,
                153,
                218,
                255,
                16,
                132,
                4,
                142,
                123,
                216,
                219,
                233,
                248,
                89,
              ];
            };
          };
        },
        {
          name: "stakeAccountCustody";
          writable: true;
          pda: {
            seeds: [
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1100-1188)
```typescript
      name: "undelegate";
      discriminator: [131, 148, 180, 198, 91, 104, 42, 238];
      accounts: [
        {
          name: "owner";
          writable: true;
          signer: true;
        },
        {
          name: "poolData";
          writable: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "publisher";
          docs: [
            "CHECK : The publisher will be checked against data in the pool_data",
          ];
        },
        {
          name: "configAccount";
          docs: ["CHECK : This AccountInfo is safe because it's a checked PDA"];
          pda: {
            seeds: [
              {
                kind: "const";
                value: [99, 111, 110, 102, 105, 103];
              },
            ];
            program: {
              kind: "account";
              path: "stakingProgram";
            };
          };
        },
        {
          name: "stakeAccountPositions";
          writable: true;
        },
        {
          name: "stakeAccountMetadata";
          docs: ["CHECK : This AccountInfo is safe because it's a checked PDA"];
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [
                  115,
                  116,
                  97,
                  107,
                  101,
                  95,
                  109,
                  101,
                  116,
                  97,
                  100,
                  97,
                  116,
                  97,
                ];
              },
              {
                kind: "account";
                path: "stakeAccountPositions";
              },
            ];
            program: {
              kind: "account";
              path: "stakingProgram";
            };
          };
        },
        {
          name: "stakeAccountCustody";
          docs: ["CHECK : This AccountInfo is safe because it's a checked PDA"];
```

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/mathematical-representation.mdx (L9-11)
```text
<Callout type="info" title="OP-PIP-103">
  Per [OP-PIP-103](https://proposals.pyth.network/proposals/103), the reward rate $y$ is currently set to **0**. The staking and slashing mechanisms remain active.
</Callout>
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

**File:** apps/staking/src/api.ts (L399-407)
```typescript
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
