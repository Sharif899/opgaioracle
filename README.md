# OpenGradient On-Chain AI Oracle

A smart contract bridge that lets any Solidity contract consume
**TEE-verified AI inference** from OpenGradient — directly on-chain.

```
┌──────────────────┐     requestInference()     ┌─────────────────┐
│  DeFi Protocol   │ ─────────────────────────► │   OGOracle.sol  │
│  (any contract)  │                             │  (OG Testnet)   │
└──────────────────┘                             └────────┬────────┘
         ▲                                                │ InferenceRequested event
         │ onInferenceFulfilled()                        ▼
         │                                    ┌─────────────────────┐
         │                                    │  oracle_listener.py │
         └────────────────────────────────────│  (Python + web3)    │
                  result + OG proof           └──────────┬──────────┘
                                                         │ alpha.infer()
                                                         ▼
                                              ┌─────────────────────┐
                                              │  OpenGradient Hub   │
                                              │  ONNX Model (TEE)   │
                                              └─────────────────────┘
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install web3 opengradient python-dotenv py-solc-x
```

### 2. Create your `.env` file

```env
OG_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
CONTRACT_ADDRESS=                        # filled by deploy.py
```

Get testnet ETH: https://faucet.opengradient.ai

### 3. Deploy the contract

```bash
python deploy.py
```

This compiles `OGOracle.sol`, deploys to OG Testnet (Chain 10740),
and writes `CONTRACT_ADDRESS` to your `.env` automatically.

### 4. Start the oracle listener

```bash
python oracle_listener.py
```

Leave this running. It polls for `InferenceRequested` events,
runs OpenGradient inference, and fulfills each request on-chain.

### 5. Run the test suite

In a **second terminal**:

```bash
python test_oracle.py
```

Or just check contract connectivity without waiting:

```bash
python test_oracle.py --no-wait
```

---

## How to Use the Oracle in Your Own Contract

```solidity
// Import the consumer interface
interface IOGOracleConsumer {
    function onInferenceFulfilled(
        bytes32 requestId,
        int256  result,
        uint8   confidence
    ) external;
}

contract MyProtocol is IOGOracleConsumer {

    address constant ORACLE = 0x...;  // OGOracle deployed address

    function checkRisk(address position) external {
        // Features scaled by 10_000 (e.g. 1.5 → 15000)
        int256[] memory features = new int256[](5);
        features[0] = 15000;  // collateral ratio 1.5x
        features[1] = 7000;   // borrowed ratio 70%
        features[2] = 2500;   // volatility 25%
        features[3] = 11000;  // health factor 1.1
        features[4] = 30000;  // 30 days deposited

        IOGOracle(ORACLE).requestInference(
            "defi_liquidation_risk",   // model CID
            features,
            "defi_risk"                // input profile
        );
    }

    // Called by oracle when inference is ready
    function onInferenceFulfilled(
        bytes32 requestId,
        int256  result,       // scaled by 1_000_000
        uint8   confidence
    ) external {
        // result > 500_000 means probability > 0.5 → HIGH RISK
        if (result > 500_000 && confidence > 70) {
            // trigger liquidation logic
        }
    }
}
```

---

## Input Profiles

| Profile | Features | Description |
|---------|----------|-------------|
| `defi_risk` | 5 | Collateral ratio, borrow ratio, volatility, health factor, days |
| `trading_signal` | 5 | Price change, volume, RSI, MACD, market cap rank |
| `credit_score` | 5 | Payment history, utilization, account age, accounts, inquiries |
| `wallet_security` | 6 | Tx count, unique contracts, avg value, wallet age, fail rate, night ratio |
| `nft_analysis` | 7 | Self-trade ratio, price round-trip, interval std, buyer diversity... |

All features are **multiplied by 10,000** before storing on-chain.
The listener divides by 10,000 before sending to the model.

---

## Results

Results are stored on-chain **multiplied by 1,000,000**:

- Binary classifier output `0.87` → stored as `870000`
- Regression output `750` (credit score) → stored as `750000000`

Read results:

```python
result_scaled, confidence, fulfilled = oracle.functions.getResultScaled(requestId).call()
result_float = result_scaled / 1_000_000
```

Or in Solidity:

```solidity
(int256 resultScaled, uint8 confidence, bool fulfilled)
    = oracle.getResultScaled(requestId);
// resultScaled / 1_000_000 = actual float
```

---

## Models Available

Upload your own ONNX models to the Hub and use their CID.
Official models on OpenGradient Hub:

| Model CID | Description |
|-----------|-------------|
| `defi_liquidation_risk` | DeFi position liquidation risk (5 features) |
| `wallet_fraud_detector` | Wallet fraud detection (6 features) |
| `credit_score_predictor` | On-chain credit score (5 features) |
| `token_price_direction` | Token price direction (5 features → 3 class) |
| `QmRhcpDXfYCK...` | ETH 1hr volatility (official OG model) |

---

## Network Details

| Property | Value |
|----------|-------|
| Network | OpenGradient Testnet |
| Chain ID | 10740 |
| RPC | https://ogevmdevnet.opengradient.ai |
| Explorer | https://explorer.opengradient.ai |
| Faucet | https://faucet.opengradient.ai |

---

## Architecture

```
contracts/
  OGOracle.sol          Core oracle contract (deploy once, use forever)
  ↳ OGOracle            Accepts requests, stores results, calls back consumers
  ↳ DeFiRiskManager     Example consumer contract

oracle_listener.py      Python off-chain component
  ↳ Polls for events every 3 seconds
  ↳ Decodes feature vectors
  ↳ Calls OpenGradient alpha.infer()
  ↳ Writes result + OG proof hash on-chain

deploy.py               One-command deployment to OG Testnet
test_oracle.py          End-to-end test suite with 4 test cases
```

---

Built for OpenGradient · https://opengradient.ai
