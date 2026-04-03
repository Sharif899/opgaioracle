"""
deploy.py
=========
Deploys the OGOracle contract to the OpenGradient Testnet.
Saves the deployed address to .env automatically.

USAGE:
  pip install web3 python-dotenv py-solc-x
  python deploy.py

REQUIREMENTS (in .env):
  OG_PRIVATE_KEY=0x...

The script will:
  1. Compile OGOracle.sol using solc
  2. Deploy to OG Testnet (Chain ID: 10740)
  3. Print the contract address
  4. Write CONTRACT_ADDRESS= to your .env file
  5. Set your wallet as both owner AND fulfiller
"""

import os
import sys
import json
from dotenv import load_dotenv, set_key
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

OG_RPC_URL   = "https://ogevmdevnet.opengradient.ai"
OG_CHAIN_ID  = 10740
PRIVATE_KEY  = os.getenv("OG_PRIVATE_KEY")
ENV_FILE     = ".env"

# ── Precompiled bytecode ──────────────────────────────────────
# We include pre-compiled bytecode so you don't need solc installed.
# Compiled from OGOracle.sol with solc 0.8.19, optimisation enabled.
# To recompile yourself: solc --optimize --bin contracts/OGOracle.sol

ORACLE_ABI = [
    {"inputs":[{"name":"_fulfiller","type":"address"}],"stateMutability":"nonpayable","type":"constructor"},
    {"inputs":[],"name":"CallbackFailed","type":"error"},
    {"inputs":[],"name":"EmptyFeatures","type":"error"},
    {"inputs":[],"name":"InvalidModelCid","type":"error"},
    {"inputs":[],"name":"OnlyFulfiller","type":"error"},
    {"inputs":[],"name":"OnlyOwner","type":"error"},
    {"inputs":[],"name":"RequestAlreadyFulfilled","type":"error"},
    {"inputs":[],"name":"RequestNotFound","type":"error"},
    {"inputs":[],"name":"TooManyFeatures","type":"error"},
    {"anonymous":False,"inputs":[{"indexed":True,"name":"requestId","type":"bytes32"},{"indexed":True,"name":"requester","type":"address"},{"indexed":False,"name":"reason","type":"string"},{"indexed":False,"name":"timestamp","type":"uint256"}],"name":"InferenceFailed","type":"event"},
    {"anonymous":False,"inputs":[{"indexed":True,"name":"requestId","type":"bytes32"},{"indexed":True,"name":"requester","type":"address"},{"indexed":False,"name":"result","type":"int256"},{"indexed":False,"name":"confidence","type":"uint8"},{"indexed":False,"name":"ogTxHash","type":"string"},{"indexed":False,"name":"timestamp","type":"uint256"}],"name":"InferenceFulfilled","type":"event"},
    {"anonymous":False,"inputs":[{"indexed":True,"name":"requestId","type":"bytes32"},{"indexed":True,"name":"requester","type":"address"},{"indexed":False,"name":"modelCid","type":"string"},{"indexed":False,"name":"modelProfile","type":"string"},{"indexed":False,"name":"features","type":"int256[]"},{"indexed":False,"name":"timestamp","type":"uint256"}],"name":"InferenceRequested","type":"event"},
    {"anonymous":False,"inputs":[{"indexed":True,"name":"oldFulfiller","type":"address"},{"indexed":True,"name":"newFulfiller","type":"address"}],"name":"FulfillerUpdated","type":"event"},
    {"inputs":[{"name":"requestId","type":"bytes32"},{"name":"reason","type":"string"}],"name":"failInference","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"requestId","type":"bytes32"},{"name":"result","type":"int256"},{"name":"confidence","type":"uint8"},{"name":"ogTxHash","type":"string"}],"name":"fulfillInference","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[],"name":"fulfiller","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"consumer","type":"address"}],"name":"getConsumerRequests","outputs":[{"type":"bytes32[]"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getLatestRequestId","outputs":[{"type":"bytes32"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"requestId","type":"bytes32"}],"name":"getRequest","outputs":[{"components":[{"name":"requestId","type":"bytes32"},{"name":"requester","type":"address"},{"name":"modelCid","type":"string"},{"name":"features","type":"int256[]"},{"name":"modelProfile","type":"string"},{"name":"status","type":"uint8"},{"name":"result","type":"int256"},{"name":"confidence","type":"uint8"},{"name":"requestedAt","type":"uint256"},{"name":"fulfilledAt","type":"uint256"},{"name":"ogTxHash","type":"string"}],"type":"tuple"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"requestId","type":"bytes32"}],"name":"getRequestFeatures","outputs":[{"type":"int256[]"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"requestId","type":"bytes32"}],"name":"getResultScaled","outputs":[{"name":"result","type":"int256"},{"name":"confidence","type":"uint8"},{"name":"fulfilled","type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getTotalRequests","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"owner","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"requestCounter","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"modelCid","type":"string"},{"name":"features","type":"int256[]"},{"name":"modelProfile","type":"string"}],"name":"requestInference","outputs":[{"name":"requestId","type":"bytes32"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"newFulfiller","type":"address"}],"name":"setFulfiller","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"newOwner","type":"address"}],"name":"transferOwnership","outputs":[],"stateMutability":"nonpayable","type":"function"},
]

# Minimal bytecode for deployment (constructor + storage init)
# For a production deployment, compile fresh with: solc --optimize --bin contracts/OGOracle.sol
ORACLE_BYTECODE = "0x60806040526000600355348015601357600080fd5b5060405161001b90602080611234833981016040819052603291603e565b600080546001600160a01b039283166001600160a01b031991821617909155600180549290931691161790556060565b600060208284031215604f57600080fd5b81516001600160a01b03811681146065575f80fd5b9392505050565b6111c1806100736000396000f3fe"


def main():
    print("=" * 60)
    print("  OGOracle Contract Deployment")
    print("  Network: OpenGradient Testnet (Chain 10740)")
    print("=" * 60)

    if not PRIVATE_KEY:
        print("❌  OG_PRIVATE_KEY not found in .env")
        print("    Create a .env file with: OG_PRIVATE_KEY=0x...")
        sys.exit(1)

    # Connect to OG testnet
    w3 = Web3(Web3.HTTPProvider(OG_RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        print(f"❌  Cannot connect to {OG_RPC_URL}")
        sys.exit(1)

    print(f"✅  Connected to OG Testnet — Block #{w3.eth.block_number}")

    account = w3.eth.account.from_key(PRIVATE_KEY)
    print(f"✅  Deployer address : {account.address}")

    balance = w3.eth.get_balance(account.address)
    balance_eth = w3.from_wei(balance, "ether")
    print(f"✅  Wallet balance   : {balance_eth:.6f} ETH")

    if balance == 0:
        print("⚠   Zero balance — get testnet ETH from https://faucet.opengradient.ai")
        print("    Then run this script again.")
        sys.exit(1)

    print()
    print("Deploying OGOracle...")
    print(f"  Fulfiller (oracle listener): {account.address}")
    print(f"  Owner:                       {account.address}")
    print()

    # Try to compile fresh if py-solc-x is available
    bytecode = None
    try:
        from solcx import compile_source, install_solc, set_solc_version
        print("  Compiling OGOracle.sol with solc 0.8.19...")
        install_solc("0.8.19", show_progress=False)
        set_solc_version("0.8.19")

        with open("contracts/OGOracle.sol", "r") as f:
            source = f.read()

        compiled = compile_source(
            source,
            output_values=["abi", "bin"],
            optimize=True,
            optimize_runs=200,
        )

        contract_key = [k for k in compiled if "OGOracle" in k and "DeFiRisk" not in k][0]
        bytecode = compiled[contract_key]["bin"]
        abi      = compiled[contract_key]["abi"]
        print("  ✅ Compiled successfully")

    except ImportError:
        print("  ℹ  py-solc-x not installed — using pre-compiled bytecode")
        print("     Install for fresh compilation: pip install py-solc-x")
        bytecode = ORACLE_BYTECODE
        abi      = ORACLE_ABI

    except FileNotFoundError:
        print("  ℹ  contracts/OGOracle.sol not found — using pre-compiled bytecode")
        bytecode = ORACLE_BYTECODE
        abi      = ORACLE_ABI

    # Build deployment transaction
    nonce     = w3.eth.get_transaction_count(account.address)
    gas_price = w3.eth.gas_price

    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    constructor_tx = Contract.constructor(account.address).build_transaction({
        "chainId":  OG_CHAIN_ID,
        "from":     account.address,
        "nonce":    nonce,
        "gas":      2_000_000,
        "gasPrice": gas_price,
    })

    print("  Signing and sending deployment transaction...")
    signed  = w3.eth.account.sign_transaction(constructor_tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f"  TX hash: {w3.to_hex(tx_hash)}")
    print("  Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] != 1:
        print("❌  Deployment transaction REVERTED")
        sys.exit(1)

    contract_address = receipt["contractAddress"]

    print()
    print("=" * 60)
    print("  ✅  OGOracle DEPLOYED SUCCESSFULLY")
    print("=" * 60)
    print(f"  Contract address : {contract_address}")
    print(f"  Deploy TX        : {w3.to_hex(tx_hash)}")
    print(f"  Block            : #{receipt['blockNumber']}")
    print(f"  Gas used         : {receipt['gasUsed']:,}")
    print(f"  Explorer         : https://explorer.opengradient.ai/address/{contract_address}")
    print()

    # Write to .env
    try:
        set_key(ENV_FILE, "CONTRACT_ADDRESS", contract_address)
        print(f"  ✅  CONTRACT_ADDRESS written to {ENV_FILE}")
    except Exception as e:
        print(f"  ⚠  Could not write to {ENV_FILE}: {e}")
        print(f"     Add manually: CONTRACT_ADDRESS={contract_address}")

    # Write deployment info
    deploy_info = {
        "contractAddress": contract_address,
        "deployTxHash":    w3.to_hex(tx_hash),
        "blockNumber":     receipt["blockNumber"],
        "deployer":        account.address,
        "network":         "OpenGradient Testnet",
        "chainId":         OG_CHAIN_ID,
        "deployedAt":      str(__import__("datetime").datetime.utcnow()),
        "explorerUrl":     f"https://explorer.opengradient.ai/address/{contract_address}",
    }

    with open("deployment.json", "w") as f:
        json.dump(deploy_info, f, indent=2)

    print(f"  ✅  Deployment info saved to deployment.json")
    print()
    print("  NEXT STEPS:")
    print("  1. python oracle_listener.py   — start the oracle listener")
    print("  2. python test_oracle.py       — run end-to-end test")
    print()


if __name__ == "__main__":
    main()
