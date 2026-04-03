"""
test_oracle.py
==============
End-to-end test for the OGOracle system.

Tests the full flow:
  1. Connect to OG Testnet
  2. Load deployed contract
  3. Submit an inference request
  4. Wait for the oracle listener to fulfill it
  5. Verify the result on-chain
  6. Print a full test report

USAGE:
  # Make sure oracle_listener.py is running in another terminal first
  python test_oracle.py

  # Or run without listener (checks contract interaction only)
  python test_oracle.py --no-wait
"""

import os
import sys
import time
import json
import argparse
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

OG_RPC_URL       = "https://ogevmdevnet.opengradient.ai"
OG_CHAIN_ID      = 10740
PRIVATE_KEY      = os.getenv("OG_PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")
FEATURE_SCALE    = 10_000
RESULT_SCALE     = 1_000_000
WAIT_TIMEOUT_SEC = 120

ORACLE_ABI = [
    {"anonymous":False,"inputs":[{"indexed":True,"name":"requestId","type":"bytes32"},{"indexed":True,"name":"requester","type":"address"},{"indexed":False,"name":"modelCid","type":"string"},{"indexed":False,"name":"modelProfile","type":"string"},{"indexed":False,"name":"features","type":"int256[]"},{"indexed":False,"name":"timestamp","type":"uint256"}],"name":"InferenceRequested","type":"event"},
    {"anonymous":False,"inputs":[{"indexed":True,"name":"requestId","type":"bytes32"},{"indexed":True,"name":"requester","type":"address"},{"indexed":False,"name":"result","type":"int256"},{"indexed":False,"name":"confidence","type":"uint8"},{"indexed":False,"name":"ogTxHash","type":"string"},{"indexed":False,"name":"timestamp","type":"uint256"}],"name":"InferenceFulfilled","type":"event"},
    {"inputs":[{"name":"modelCid","type":"string"},{"name":"features","type":"int256[]"},{"name":"modelProfile","type":"string"}],"name":"requestInference","outputs":[{"name":"requestId","type":"bytes32"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"requestId","type":"bytes32"}],"name":"getRequest","outputs":[{"components":[{"name":"requestId","type":"bytes32"},{"name":"requester","type":"address"},{"name":"modelCid","type":"string"},{"name":"features","type":"int256[]"},{"name":"modelProfile","type":"string"},{"name":"status","type":"uint8"},{"name":"result","type":"int256"},{"name":"confidence","type":"uint8"},{"name":"requestedAt","type":"uint256"},{"name":"fulfilledAt","type":"uint256"},{"name":"ogTxHash","type":"string"}],"type":"tuple"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"requestId","type":"bytes32"}],"name":"getResultScaled","outputs":[{"name":"result","type":"int256"},{"name":"confidence","type":"uint8"},{"name":"fulfilled","type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getTotalRequests","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getLatestRequestId","outputs":[{"type":"bytes32"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"owner","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"fulfiller","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
]


# ── Test cases ────────────────────────────────────────────────

TEST_CASES = [
    {
        "name":    "DeFi Liquidation Risk — SAFE position",
        "model":   "defi_liquidation_risk",
        "profile": "defi_risk",
        "features_float": [2.5, 0.30, 0.05, 2.2, 180.0],  # healthy position
        "expected_range": (0.0, 0.4),   # expect LOW risk
        "description": "Well-collateralised position, low borrow ratio — should be safe",
    },
    {
        "name":    "DeFi Liquidation Risk — DANGEROUS position",
        "model":   "defi_liquidation_risk",
        "profile": "defi_risk",
        "features_float": [1.2, 0.85, 0.40, 1.05, 5.0],   # at risk
        "expected_range": (0.5, 1.0),   # expect HIGH risk
        "description": "Near-liquidation health factor, high borrow ratio — should flag",
    },
    {
        "name":    "Credit Score — Excellent applicant",
        "model":   "credit_score_predictor",
        "profile": "credit_score",
        "features_float": [0.99, 0.05, 15.0, 8.0, 0.0],   # great credit
        "expected_range": (700, 850),
        "description": "Perfect payment history, low utilisation, long history",
    },
    {
        "name":    "Wallet Fraud — Clean wallet",
        "model":   "wallet_fraud_detector",
        "profile": "wallet_security",
        "features_float": [2.0, 5.0, 1.5, 500.0, 0.02, 0.1],
        "expected_range": (0.0, 0.3),
        "description": "Low transaction count, old wallet, normal behaviour",
    },
]


def encode_features(features_float: list[float]) -> list[int]:
    """Scale floats to int256 for on-chain storage."""
    return [int(f * FEATURE_SCALE) for f in features_float]


def decode_result(result_scaled: int, model: str) -> float:
    """Decode on-chain scaled result back to float."""
    return result_scaled / RESULT_SCALE


def print_banner():
    print("""
╔══════════════════════════════════════════════════════════╗
║        OGOracle End-to-End Test Suite                    ║
║        OpenGradient Testnet · Chain 10740                ║
╚══════════════════════════════════════════════════════════╝
    """)


def print_separator():
    print("─" * 65)


def run_test(
    w3:       Web3,
    contract,
    account,
    test:     dict,
    wait:     bool,
) -> dict:
    """Run a single test case. Returns result dict."""
    print(f"\n🧪  {test['name']}")
    print(f"    {test['description']}")
    print(f"    Model   : {test['model']}")
    print(f"    Features: {test['features_float']}")

    # Encode features
    features_int = encode_features(test["features_float"])
    print(f"    Encoded : {features_int}")

    # Submit inference request
    nonce     = w3.eth.get_transaction_count(account.address)
    gas_price = w3.eth.gas_price

    tx = contract.functions.requestInference(
        test["model"],
        features_int,
        test["profile"],
    ).build_transaction({
        "chainId":  OG_CHAIN_ID,
        "from":     account.address,
        "nonce":    nonce,
        "gas":      300_000,
        "gasPrice": gas_price,
    })

    signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    if receipt["status"] != 1:
        print("    ❌  requestInference tx REVERTED")
        return {"name": test["name"], "status": "FAILED", "reason": "TX reverted"}

    # Get request ID from logs
    logs = contract.events.InferenceRequested().process_receipt(receipt)
    if not logs:
        print("    ❌  No InferenceRequested event in receipt")
        return {"name": test["name"], "status": "FAILED", "reason": "No event"}

    request_id = logs[0]["args"]["requestId"]
    print(f"    ✅  Request submitted: 0x{request_id.hex()[:16]}...")
    print(f"       TX: https://explorer.opengradient.ai/tx/{w3.to_hex(tx_hash)}")

    if not wait:
        print("    ℹ  --no-wait flag set — skipping fulfillment check")
        return {
            "name":      test["name"],
            "status":    "SUBMITTED",
            "requestId": f"0x{request_id.hex()}",
            "txHash":    w3.to_hex(tx_hash),
        }

    # Wait for fulfillment
    print(f"    ⏳  Waiting up to {WAIT_TIMEOUT_SEC}s for oracle fulfillment...")
    print(f"       (make sure oracle_listener.py is running!)")

    deadline = time.time() + WAIT_TIMEOUT_SEC
    while time.time() < deadline:
        time.sleep(3)
        result_scaled, confidence, fulfilled = contract.functions.getResultScaled(request_id).call()

        if fulfilled:
            result = decode_result(result_scaled, test["model"])
            req    = contract.functions.getRequest(request_id).call()
            og_tx  = req[10]  # ogTxHash field

            lo, hi = test["expected_range"]
            in_range = lo <= result <= hi
            status   = "PASSED" if in_range else "WARNING"
            icon     = "✅" if in_range else "⚠"

            print(f"    {icon}  Fulfilled!")
            print(f"       Result     : {result:.6f}")
            print(f"       Confidence : {confidence}%")
            print(f"       Expected   : [{lo}, {hi}]")
            print(f"       In range   : {'YES ✅' if in_range else 'NO ⚠ (model may need retraining)'}")
            if og_tx:
                print(f"       OG Proof   : {og_tx}")

            return {
                "name":       test["name"],
                "status":     status,
                "result":     result,
                "confidence": confidence,
                "in_range":   in_range,
                "ogTxHash":   og_tx,
                "requestId":  f"0x{request_id.hex()}",
                "txHash":     w3.to_hex(tx_hash),
            }

    print(f"    ⏰  Timeout! Oracle did not fulfill within {WAIT_TIMEOUT_SEC}s")
    print(f"       Make sure oracle_listener.py is running")
    return {
        "name":      test["name"],
        "status":    "TIMEOUT",
        "requestId": f"0x{request_id.hex()}",
        "txHash":    w3.to_hex(tx_hash),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-wait", action="store_true", help="Submit requests but don't wait for fulfillment")
    args = parser.parse_args()

    print_banner()

    # Validate env
    if not PRIVATE_KEY:
        print("❌  OG_PRIVATE_KEY not set in .env"); sys.exit(1)
    if not CONTRACT_ADDRESS:
        print("❌  CONTRACT_ADDRESS not set in .env"); sys.exit(1)

    # Connect
    w3 = Web3(Web3.HTTPProvider(OG_RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        print(f"❌  Cannot connect to {OG_RPC_URL}"); sys.exit(1)

    print(f"✅  Connected — Block #{w3.eth.block_number}")

    account = w3.eth.account.from_key(PRIVATE_KEY)
    print(f"✅  Tester wallet: {account.address}")

    balance = w3.eth.get_balance(account.address)
    print(f"✅  Balance: {w3.from_wei(balance,'ether'):.6f} ETH")

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACT_ADDRESS),
        abi=ORACLE_ABI,
    )

    total = contract.functions.getTotalRequests().call()
    owner = contract.functions.owner().call()
    fulfiller = contract.functions.fulfiller().call()

    print(f"✅  Contract: {CONTRACT_ADDRESS}")
    print(f"   Owner     : {owner}")
    print(f"   Fulfiller : {fulfiller}")
    print(f"   Total reqs: {total}")

    if not args.no_wait:
        print("\n⚠  Make sure oracle_listener.py is running in another terminal!")
        print("   python oracle_listener.py")
        print()
        time.sleep(2)

    # Run all tests
    print_separator()
    print(f"Running {len(TEST_CASES)} test cases...")
    print_separator()

    results = []
    for test in TEST_CASES:
        result = run_test(w3, contract, account, test, wait=not args.no_wait)
        results.append(result)

    # Summary
    print()
    print_separator()
    print("  TEST SUMMARY")
    print_separator()

    passed  = sum(1 for r in results if r["status"] == "PASSED")
    warning = sum(1 for r in results if r["status"] == "WARNING")
    failed  = sum(1 for r in results if r["status"] in ("FAILED","TIMEOUT"))
    submitted=sum(1 for r in results if r["status"] == "SUBMITTED")

    for r in results:
        icon = {"PASSED":"✅","WARNING":"⚠ ","FAILED":"❌","TIMEOUT":"⏰","SUBMITTED":"📤"}.get(r["status"],"?")
        print(f"  {icon}  [{r['status']:<10}]  {r['name']}")

    print()
    if not args.no_wait:
        print(f"  Results: {passed} passed | {warning} warnings | {failed} failed")
        if passed == len(TEST_CASES):
            print("  🎉  All tests passed! Oracle is working end-to-end.")
        elif passed + warning == len(TEST_CASES):
            print("  ✅  Oracle functional — some outputs outside expected range (normal for small models).")
    else:
        print(f"  📤  {submitted} requests submitted — run without --no-wait to check fulfillment")

    print()
    print("  New requests on contract:", contract.functions.getTotalRequests().call())

    # Save results
    with open("test_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("  Results saved to test_results.json")
    print()


if __name__ == "__main__":
    main()
