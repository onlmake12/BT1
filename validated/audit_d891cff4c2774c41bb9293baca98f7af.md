### Title
Users With UNLOCKED Positions Can Withdraw Before `slash` Is Applied, Escaping OIS Slashing - (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`, `governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

The Pyth OIS integrity pool applies slashing lazily, per-user, via a dedicated `slash` instruction. The `withdraw_stake` instruction in the staking program does not require that pending `SlashEvent`s have been applied to a user's account before withdrawal. A user with an UNLOCKED position (cooldown ended) can call `withdraw_stake` before anyone calls `slash` on their account, escaping the slash entirely. This is structurally analogous to the Kinetiq H-02 pattern: a snapshot of the user's token amount is taken at position-creation time and is not adjusted for post-creation slashing at withdrawal time.

---

### Finding Description

The integrity pool program uses a two-step slash mechanism:

1. **`create_slash_event`** — creates a `SlashEvent` PDA (keyed by publisher + index) storing `epoch`, `slash_ratio`, and `slash_custody`. This is a global record; no tokens move yet.
2. **`slash`** — called per-user, per-slash-event. It modifies `stake_account_positions` (writable) to reduce position amounts and transfers tokens from `stake_account_custody` to `slash_custody`. It updates `delegation_record.next_slash_event_index` to mark the event as applied.

The `DelegationRecord` struct tracks `next_slash_event_index` to record which slash events have been applied to a given user: [1](#0-0) 

The `slash` instruction accounts confirm it is the only mechanism that transfers tokens to `slash_custody` and reduces position amounts: [2](#0-1) 

The `withdraw_stake` instruction in the staking program takes only: `owner`, `destination`, `stake_account_positions`, `stake_account_metadata`, `stake_account_custody`, `custody_authority`, `config`, `token_program`. It does **not** include `delegation_record`, `slash_event`, or any account that would allow it to check `next_slash_event_index`: [3](#0-2) 

Because `withdraw_stake` has no visibility into pending slash events, it cannot enforce that all applicable `slash` calls have been made before the user withdraws.

The `advance_delegation_record` instruction — which processes rewards — also does **not** include `slash_custody` in its accounts, confirming it does not apply slashes: [4](#0-3) 

A `Position` stores a fixed `amount` field set at creation time: [5](#0-4) 

Once a position reaches `UNLOCKED` state (i.e., `unlocking_start + 1 <= current_epoch`), the user can call `withdraw_stake` to transfer the full `amount` from custody to their wallet: [6](#0-5) 

---

### Impact Explanation

A user with an UNLOCKED position who observes a `create_slash_event` transaction on-chain can immediately submit `withdraw_stake` before the `slash` instruction is called on their account. They receive the full pre-slash token amount. When `slash` is subsequently called on their (now-empty) custody account, the token transfer to `slash_custody` fails or is a no-op, meaning:

- The user escapes the slash entirely.
- The `slash_custody` (DAO treasury) receives fewer tokens than the `slash_ratio` dictates.
- Other users who do not withdraw in time are slashed at the full rate, bearing a disproportionate share of the penalty.

This directly mirrors the Kinetiq H-02 pattern: early actors (here, fast withdrawers) take more than their fair share at the expense of slower actors, and the slashing mechanism's security guarantee is undermined.

---

### Likelihood Explanation

- Slash events are rare but high-stakes (up to 5% of pool stake per the rulebook).
- The `slash` instruction must be called individually for each staker's account; with many stakers, this takes multiple transactions across multiple blocks.
- Any user monitoring the chain (e.g., via websocket subscription to `create_slash_event` account creation) can detect the event and submit `withdraw_stake` within the same or next block.
- The `slash` instruction is permissionless (`signer` with no role constraint), so there is no privileged party who can atomically apply it to all users simultaneously.
- Sophisticated stakers (e.g., large delegators) have strong financial incentive to monitor and act. [7](#0-6) 

---

### Recommendation

`withdraw_stake` should enforce that all pending slash events (up to `pool_config.num_slash_events` or equivalent) have been applied to the user's `delegation_record` before allowing withdrawal. Concretely:

- Pass `delegation_record` and `pool_data` (or `pool_config`) as accounts to `withdraw_stake`.
- Assert `delegation_record.next_slash_event_index >= pool_data.num_slash_events_for_publisher` (or the global slash event count for the relevant publisher) before transferring tokens.

Alternatively, the `slash` instruction could be made a prerequisite CPI within `withdraw_stake` itself, atomically applying any unapplied slash events before the withdrawal proceeds.

---

### Proof of Concept

1. Alice has 1,000 PYTH staked to publisher P. Her position enters UNLOCKED state (cooldown ended).
2. The Pythian Council calls `create_slash_event` for publisher P with `slash_ratio = 5%` (50 PYTH should be slashed from Alice).
3. Alice observes the `create_slash_event` transaction on-chain and immediately calls `withdraw_stake`.
4. `withdraw_stake` reads Alice's position `amount = 1000`, transfers 1,000 PYTH from her custody to her wallet. No check on `delegation_record.next_slash_event_index`.
5. Someone later calls `slash` on Alice's account. Her custody is now empty; the slash transfer to `slash_custody` fails. Alice has escaped the 50 PYTH slash.
6. Bob, who did not withdraw in time, has `slash` applied to his account and loses 5% of his stake. Bob bears a disproportionate share of the penalty relative to Alice. [8](#0-7) [9](#0-8)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L209-270)
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
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L762-900)
```json
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
          "name": "slash_event",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [115, 108, 97, 115, 104, 95, 101, 118, 101, 110, 116]
              },
              {
                "kind": "account",
                "path": "publisher"
              },
              {
                "kind": "arg",
                "path": "index"
              }
            ]
          }
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
          "docs": [
            "CHECK : The publisher will be checked in the staking program"
          ],
          "name": "publisher"
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
              "kind": "const",
              "value": [
                12, 74, 158, 192, 43, 86, 104, 29, 164, 155, 4, 186, 155, 36,
                207, 137, 253, 128, 249, 44, 241, 145, 227, 125, 189, 51, 111,
                70, 231, 183, 19, 217
              ]
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
          "name": "stake_account_custody",
          "pda": {
            "program": {
              "kind": "const",
              "value": [
                12, 74, 158, 192, 43, 86, 104, 29, 164, 155, 4, 186, 155, 36,
                207, 137, 253, 128, 249, 44, 241, 145, 227, 125, 189, 51, 111,
                70, 231, 183, 19, 217
              ]
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
          },
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "config_account",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
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

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1524-1542)
```json
      "name": "SlashEvent",
      "type": {
        "fields": [
          {
            "name": "epoch",
            "type": "u64"
          },
          {
            "name": "slash_ratio",
            "type": "u64"
          },
          {
            "name": "slash_custody",
            "type": "pubkey"
          }
        ],
        "kind": "struct"
      }
    },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1771-1807)
```json
            "seeds": [
              {
                "kind": "const",
                "value": [97, 117, 116, 104, 111, 114, 105, 116, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          }
        },
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        }
      ],
      "args": [
        {
          "name": "amount",
          "type": "u64"
        }
      ],
      "discriminator": [153, 8, 22, 138, 105, 176, 87, 66],
      "name": "withdraw_stake"
    }
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1940-1974)
```json
      "docs": [
        "This represents a staking position, i.e. an amount that someone has staked to a particular",
        "target. This is one of the core pieces of our staking design, and stores all",
        "of the state related to a position The voting position is a position where the",
        "target_with_parameters is VOTING"
      ],
      "name": "Position",
      "type": {
        "fields": [
          {
            "name": "amount",
            "type": "u64"
          },
          {
            "name": "activation_epoch",
            "type": "u64"
          },
          {
            "name": "unlocking_start",
            "type": {
              "option": "u64"
            }
          },
          {
            "name": "target_with_parameters",
            "type": {
              "defined": {
                "name": "TargetWithParameters"
              }
            }
          }
        ],
        "kind": "struct"
      }
    },
```

**File:** governance/pyth_staking_sdk/src/utils/position.ts (L17-37)
```typescript
export const getPositionState = (
  position: Position,
  currentEpoch: bigint,
): PositionState => {
  if (currentEpoch < position.activationEpoch) {
    return PositionState.LOCKING;
  }
  if (!position.unlockingStart) {
    return PositionState.LOCKED;
  }
  const hasActivated = position.activationEpoch <= currentEpoch;
  const unlockStarted = position.unlockingStart <= currentEpoch;
  const unlockEnded = position.unlockingStart + 1n <= currentEpoch;

  if (hasActivated && !unlockStarted) {
    return PositionState.PREUNLOCKING;
  } else if (unlockStarted && !unlockEnded) {
    return PositionState.UNLOCKING;
  } else {
    return PositionState.UNLOCKED;
  }
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L350-425)
```typescript
      name: "createSlashEvent";
      discriminator: [7, 214, 12, 127, 239, 247, 253, 117];
      accounts: [
        {
          name: "payer";
          writable: true;
          signer: true;
        },
        {
          name: "rewardProgramAuthority";
          signer: true;
          relations: ["poolConfig"];
        },
        {
          name: "slashCustody";
          relations: ["poolConfig"];
        },
        {
          name: "poolData";
          writable: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          writable: true;
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
          name: "slashEvent";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [115, 108, 97, 115, 104, 95, 101, 118, 101, 110, 116];
              },
              {
                kind: "account";
                path: "publisher";
              },
              {
                kind: "arg";
                path: "index";
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
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
      ];
      args: [
        {
          name: "index";
          type: "u64";
        },
        {
          name: "slashRatio";
          type: "u64";
        },
      ];
    },
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1803-1822)
```typescript
    {
      name: "slashEvent";
      type: {
        kind: "struct";
        fields: [
          {
            name: "epoch";
            type: "u64";
          },
          {
            name: "slashRatio";
            type: "u64";
          },
          {
            name: "slashCustody";
            type: "pubkey";
          },
        ];
      };
    },
```
