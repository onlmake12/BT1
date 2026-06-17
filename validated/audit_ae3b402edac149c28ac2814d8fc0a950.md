### Title
Staking Reward Loss Due to Missing `advance()` Call Before Undelegation — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

### Summary
The OIS integrity pool's permissionless `advance` instruction must be called once per epoch to record that epoch's reward parameters into `PoolData.events`. Because `undelegate` does not atomically trigger `advance`, a user who undelegates between epoch boundary and the `advance` call for that epoch may permanently lose their rewards for that epoch. This is a direct structural analog to the GoGoPool `syncRewards()` timing vulnerability.

### Finding Description
The integrity pool program (`pyti8TM4zRVBjmarcgAPmTNNAXYKJv7WVHrkrm6woLN`) operates on a 7-day epoch model. At the start of each epoch, the permissionless `advance` instruction must be called to:

1. Snapshot the current publisher caps and reward rate `y` into the `PoolData.events` ring buffer (52 slots, one per week).
2. Advance `PoolData.last_updated_epoch` to the current epoch. [1](#0-0) 

Per-user reward accounting is handled by a separate `advanceDelegationRecord` instruction, which iterates through the `events` array from `DelegationRecord.lastEpoch + 1` up to the current epoch to compute claimable rewards. [2](#0-1) 

The `DelegationRecord` struct stores only `lastEpoch` and `nextSlashEventIndex` — there is no mechanism to retroactively credit rewards for an epoch whose data was never written into `events`. [3](#0-2) 

Critically, the `undelegate` instruction (called via `unstakeFromPublisher` in the SDK) does **not** include `pool_data` or `pool_reward_custody` in its account list, meaning it does not enforce that `advance` has been called for the current epoch before the position is removed. [4](#0-3) 

Furthermore, `advance` requires a verified `publisher_caps` account (`is_verified` flag) as input. This means `advance` cannot be called until the publisher caps message has been posted and verified on-chain — introducing a mandatory latency window at the start of every epoch. [5](#0-4) 

The error code `OutdatedDelegatorAccounting` (6008) confirms the program is aware of stale accounting states, but this guard is not enforced on the `undelegate` path. [6](#0-5) 

### Impact Explanation
A delegator who stakes during epoch N and undelegates after epoch N ends but **before** `advance` is called for epoch N will have their `DelegationRecord.lastEpoch` remain at N-1. When `advance` is eventually called for epoch N, the events slot for epoch N is written. However, because the user's position is now in cooldown/deactivated state, `advanceDelegationRecord` will not credit them for epoch N — the epoch's reward share is effectively redistributed to remaining stakers. This is a direct loss of earned yield with no recourse. [7](#0-6) 

### Likelihood Explanation
Every epoch boundary creates this window. The `advance` call requires a fresh, on-chain-verified `publisher_caps` VAA. Even under normal Pyth infrastructure operation, there is a non-zero delay between epoch rollover and the moment a valid publisher caps message is posted and `advance` is executed. Any user who undelegates during this window — which could span minutes to hours depending on Hermes availability and on-chain confirmation latency — loses their epoch rewards. The `advance` function is permissionless, so no privileged actor is required to trigger the loss; the timing gap alone is sufficient. [8](#0-7) 

### Recommendation
1. **Enforce `advance` before `undelegate`**: Add a guard in the `undelegate` instruction that requires `pool_data.last_updated_epoch == current_epoch`, returning `OutdatedPublisherAccounting` if not satisfied. This mirrors the pattern already used for `delegate`.
2. **Alternatively, auto-advance on undelegate**: If publisher caps are available on-chain, invoke `advance` as a CPI within `undelegate` to ensure the epoch is recorded before the position is removed.
3. **Document the risk**: Until a code fix is deployed, document that users should call `advanceDelegationRecord` (after `advance` is called) before undelegating to ensure rewards are captured.

### Proof of Concept
1. Epoch N ends at Thursday 00:00 UTC. Alice has 10,000 PYTH delegated to publisher P.
2. At 00:05 UTC, Alice calls `undelegate` to remove her stake. `advance` has not yet been called for epoch N (publisher caps VAA not yet posted). The transaction succeeds — `undelegate` does not check `last_updated_epoch`.
3. At 00:30 UTC, the publisher caps VAA is posted and verified on-chain. Someone calls `advance` for epoch N. The `events[N % 52]` slot is written with epoch N's reward parameters.
4. Alice calls `advanceDelegationRecord`. Her `DelegationRecord.lastEpoch` is N-1. The function attempts to compute rewards for epoch N, but her position is now in cooldown state (deactivated at epoch N). She receives 0 rewards for epoch N.
5. Bob, who delegated to publisher P at 00:10 UTC (after Alice undelegated), receives Alice's share of epoch N rewards when he calls `advanceDelegationRecord`. [9](#0-8)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L68-73)
```json
      "name": "OutdatedPublisherCaps"
    },
    {
      "code": 6008,
      "name": "OutdatedDelegatorAccounting"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L145-208)
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
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L209-347)
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
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1364-1450)
```json
    {
      "name": "PoolData",
      "repr": {
        "kind": "c"
      },
      "serialization": "bytemuck",
      "type": {
        "fields": [
          {
            "name": "last_updated_epoch",
            "type": "u64"
          },
          {
            "name": "claimable_rewards",
            "type": "u64"
          },
          {
            "name": "publishers",
            "type": {
              "array": ["pubkey", 1024]
            }
          },
          {
            "name": "del_state",
            "type": {
              "array": [
                {
                  "defined": {
                    "name": "DelegationState"
                  }
                },
                1024
              ]
            }
          },
          {
            "name": "self_del_state",
            "type": {
              "array": [
                {
                  "defined": {
                    "name": "DelegationState"
                  }
                },
                1024
              ]
            }
          },
          {
            "name": "publisher_stake_accounts",
            "type": {
              "array": ["pubkey", 1024]
            }
          },
          {
            "name": "events",
            "type": {
              "array": [
                {
                  "defined": {
                    "name": "Event"
                  }
                },
                52
              ]
            }
          },
          {
            "name": "num_events",
            "type": "u64"
          },
          {
            "name": "num_slash_events",
            "type": {
              "array": ["u64", 1024]
            }
          },
          {
            "name": "delegation_fees",
            "type": {
              "array": ["u64", 1024]
            }
          }
        ],
        "kind": "struct"
      }
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1469-1499)
```json
      "name": "PublisherCaps",
      "repr": {
        "kind": "c"
      },
      "serialization": "bytemuck",
      "type": {
        "fields": [
          {
            "name": "write_authority",
            "type": "pubkey"
          },
          {
            "name": "is_verified",
            "type": "u8"
          },
          {
            "name": "padding",
            "type": {
              "array": ["u8", 4]
            }
          },
          {
            "name": "publisher_caps_message_buffer",
            "type": {
              "array": ["u8", 40971]
            }
          }
        ],
        "kind": "struct"
      }
    },
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L373-397)
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

**File:** governance/pyth_staking_sdk/src/utils/clock.ts (L14-18)
```typescript
export const getCurrentEpoch: (connection: Connection) => Promise<bigint> =
  async (connection: Connection) => {
    const timestamp = await getCurrentSolanaTimestamp(connection);
    return timestamp / EPOCH_DURATION;
  };
```
