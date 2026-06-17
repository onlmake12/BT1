### Title
Expired OIS Rewards Permanently Locked in `pool_reward_custody` with No Reclaim Mechanism — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The Oracle Integrity Staking (OIS) program stores per-epoch reward event data in a fixed-size circular buffer of 52 entries (`poolData.events[52]`). When a staker fails to call `advance_delegation_record` within 52 epochs (~1 year), the event data required to compute their rewards is overwritten. The reward tokens that were transferred into `pool_reward_custody` for those epochs remain there permanently. The integrity pool program exposes no instruction to reclaim or redistribute these expired reward tokens, making them permanently inaccessible.

---

### Finding Description

The `poolData` account contains a fixed-size `events` array of 52 entries: [1](#0-0) 

Each epoch, the `advance` instruction writes a new event into this circular buffer and transfers PYTH tokens from the reward program authority into `pool_reward_custody`: [2](#0-1) 

Stakers claim their rewards by calling `advance_delegation_record`, which reads the event data and transfers tokens from `pool_reward_custody` to `stake_account_custody`: [3](#0-2) 

The SDK computes reward expiry as `lowestEpoch + 53n` — exactly matching the 52-entry buffer size: [4](#0-3) 

The UI warns users that "Rewards expire one year from the epoch in which they were earned": [5](#0-4) 

Once the circular buffer overwrites the event data for a given epoch, `advance_delegation_record` can no longer compute or pay out those rewards. The PYTH tokens that were deposited into `pool_reward_custody` for those epochs remain there indefinitely.

Reviewing the full instruction set of the integrity pool program, there is no `reclaim_expired_rewards`, `withdraw_from_pool_reward_custody`, or equivalent instruction: [6](#0-5) 

Even the `reward_program_authority` (a privileged role) has no path to recover these tokens. The `pool_reward_custody` PDA is controlled exclusively by the integrity pool program, and the only instruction that moves tokens out of it is `advance_delegation_record` — which requires valid event data that no longer exists for expired epochs.

---

### Impact Explanation

PYTH tokens deposited into `pool_reward_custody` for reward epochs that are never claimed within the 52-epoch window become permanently locked. There is no on-chain mechanism for any party — including the `reward_program_authority` — to retrieve them. This is a direct, permanent loss of PYTH tokens from the reward pool. The magnitude scales with the reward rate `y` and the number of inactive stakers who miss the claim window.

---

### Likelihood Explanation

Any staker who delegates to a publisher and then becomes inactive for approximately one year (52 epochs × ~7 days/epoch) will have their earned rewards expire. This is a realistic scenario for long-term token holders who stake and forget. The UI warning exists but is passive — there is no on-chain enforcement that forces a claim before expiry, and no automated keeper mechanism is guaranteed to claim on behalf of users.

---

### Recommendation

Add a `reclaim_expired_rewards` instruction (callable by `reward_program_authority`) that transfers tokens from `pool_reward_custody` back to the reward program authority's account for any epochs whose event data has been overwritten by the circular buffer. Alternatively, modify the `advance` instruction to track and subtract expired reward allocations from `pool_reward_custody` before depositing new rewards, so that expired tokens are recycled into future reward epochs rather than accumulating as dead weight.

---

### Proof of Concept

1. Reward rate `y` is set to a non-zero value by `reward_program_authority` via `update_y`.
2. Staker A delegates PYTH to a publisher in epoch `E`. The `advance` instruction runs each epoch, depositing tokens into `pool_reward_custody` and recording event data in `poolData.events[E % 52]`.
3. Staker A does not call `advance_delegation_record` for 53 epochs.
4. In epoch `E + 53`, the `advance` instruction overwrites `poolData.events[E % 52]` with new data.
5. Staker A attempts to call `advance_delegation_record` — the event data for epoch `E` is gone; the instruction either reverts or returns 0 rewards.
6. The PYTH tokens deposited into `pool_reward_custody` for Staker A's share of epoch `E`'s rewards remain in `pool_reward_custody` permanently.
7. No instruction in the integrity pool program IDL allows any party to withdraw these tokens. [7](#0-6) [8](#0-7)

### Citations

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1643-1660)
```typescript
    },
    {
      name: "poolData";
      serialization: "bytemuck";
      repr: {
        kind: "c";
      };
      type: {
        kind: "struct";
        fields: [
          {
            name: "lastUpdatedEpoch";
            type: "u64";
          },
          {
            name: "claimableRewards";
            type: "u64";
          },
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1700-1710)
```typescript
            name: "events";
            type: {
              array: [
                {
                  defined: {
                    name: "event";
                  };
                },
                52,
              ];
            };
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1-35)
```json
{
  "accounts": [
    {
      "discriminator": [203, 185, 161, 226, 129, 251, 132, 155],
      "name": "DelegationRecord"
    },
    {
      "discriminator": [149, 8, 156, 202, 160, 252, 176, 217],
      "name": "GlobalConfig"
    },
    {
      "discriminator": [26, 108, 14, 123, 116, 230, 129, 43],
      "name": "PoolConfig"
    },
    {
      "discriminator": [155, 28, 220, 37, 221, 242, 70, 167],
      "name": "PoolData"
    },
    {
      "discriminator": [85, 195, 241, 79, 124, 192, 79, 11],
      "name": "PositionData"
    },
    {
      "discriminator": [5, 87, 155, 44, 121, 90, 35, 134],
      "name": "PublisherCaps"
    },
    {
      "discriminator": [60, 32, 32, 44, 93, 234, 234, 89],
      "name": "SlashEvent"
    },
    {
      "discriminator": [157, 23, 139, 117, 181, 44, 197, 130],
      "name": "TargetMetadata"
    }
  ],
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L145-207)
```json
  "instructions": [
    {
      "accounts": [
        {
          "name": "signer",
          "signer": true
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "publisher_caps"
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "pool_reward_custody",
          "pda": {
            "program": {
              "kind": "const",
              "value": [
                140, 151, 37, 143, 78, 36, 137, 241, 187, 61, 16, 41, 20, 142,
                13, 131, 11, 90, 19, 153, 218, 255, 16, 132, 4, 142, 123, 216,
                219, 233, 248, 89
              ]
            },
            "seeds": [
              {
                "kind": "account",
                "path": "pool_config"
              },
              {
                "kind": "const",
                "value": [
                  6, 221, 246, 225, 215, 101, 161, 147, 217, 203, 225, 70, 206,
                  235, 121, 172, 28, 180, 133, 237, 95, 91, 55, 145, 58, 140,
                  245, 133, 126, 255, 0, 169
                ]
              },
              {
                "account": "PoolConfig",
                "kind": "account",
                "path": "pool_config.pyth_token_mint"
              }
            ]
          },
          "writable": true
        }
      ],
      "args": [],
      "discriminator": [7, 56, 108, 201, 36, 20, 57, 89],
      "name": "advance"
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L209-346)
```json
    {
      "accounts": [
        {
          "name": "payer",
          "signer": true,
          "writable": true
        },
        {
          "name": "stake_account_positions"
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "pool_reward_custody",
          "pda": {
            "program": {
              "kind": "const",
              "value": [
                140, 151, 37, 143, 78, 36, 137, 241, 187, 61, 16, 41, 20, 142,
                13, 131, 11, 90, 19, 153, 218, 255, 16, 132, 4, 142, 123, 216,
                219, 233, 248, 89
              ]
            },
            "seeds": [
              {
                "kind": "account",
                "path": "pool_config"
              },
              {
                "kind": "const",
                "value": [
                  6, 221, 246, 225, 215, 101, 161, 147, 217, 203, 225, 70, 206,
                  235, 121, 172, 28, 180, 133, 237, 95, 91, 55, 145, 58, 140,
                  245, 133, 126, 255, 0, 169
                ]
              },
              {
                "account": "PoolConfig",
                "kind": "account",
                "path": "pool_config.pyth_token_mint"
              }
            ]
          },
          "writable": true
        },
        {
          "name": "stake_account_custody",
          "pda": {
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
          },
          "writable": true
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "name": "publisher_stake_account_positions",
          "optional": true
        },
        {
          "name": "publisher_stake_account_custody",
          "optional": true,
          "pda": {
            "seeds": [
              {
                "kind": "const",
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

**File:** apps/staking/src/components/AccountSummary/index.tsx (L237-246)
```typescript
            {...(expiringRewards !== undefined &&
              availableRewards > 0n && {
                warning: (
                  <>
                    Rewards expire one year from the epoch in which they were
                    earned. You have rewards expiring on{" "}
                    <Date>{expiringRewards}</Date>.
                  </>
                ),
              })}
```
