"""
oracle_listener.py
==================
OpenGradient On-Chain AI Oracle — Python Listener

This script runs continuously, monitoring the OpenGradient testnet for
InferenceRequested events. When it sees one, it:

  1. Decodes the request (model CID + feature vector)
  2. Scales the features back to floats
  3. Runs inference through OpenGradient (TEE-verified)
  4. Calls fulfillInference() on the contract with the result
  5. Logs the on-chain settlement tx hash

HOW TO RUN:
  pip install web3 opengradient python-dotenv
  cp .env.example .env        # fill in your keys
  python oracle_listener.py

REQUIREMENTS:
  - OG_PRIVATE_KEY  : Wallet private key for signing txs on OG testnet
  - CONTRACT_ADDRESS: Deployed OGOracle contract address
  - Python 3.10+
"""

import os
import sys
import time
import asyncio
import logging
import json
from datetime import datetime
from decimal import Decimal
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
import opengradient as og

load_dotenv()

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("OGOracle")

# ── Config ────────────────────────────────────────────────────
OG_RPC_URL        = "https://ogevmdevnet.opengradient.ai"
OG_CHAIN_ID       = 10740
POLL_INTERVAL_SEC = 3          # how often to poll for new events
FEATURE_SCALE     = 10_000     # on-chain ints are divided by this to get floats
RESULT_SCALE      = 1_000_000  # floats are multiplied by this before storing on-chain

PRIVATE_KEY       = os.getenv("OG_PRIVATE_KEY")
CONTRACT_ADDRESS  = os.getenv("CONTRACT_ADDRESS")
MODEL_HUB_EMAIL   = os.getenv("HUB_EMAIL",    "")
MODEL_HUB_PASS    = os.getenv("HUB_PASSWORD", "")

# ── Contract ABI (minimal — only what we need) ────────────────
ORACLE_ABI = [
    {
        "name": "InferenceRequested",
        "type": "event",
        "inputs": [
            {"name": "requestId",    "type": "bytes32", "indexed": True},
            {"name": "requester",    "type": "address", "indexed": True},
            {"name": "modelCid",     "type": "string",  "indexed": False},
            {"name": "modelProfile", "type": "string",  "indexed": False},
            {"name": "features",     "type": "int256[]","indexed": False},
            {"name": "timestamp",    "type": "uint256", "indexed": False},
        ],
    },
    {
        "name": "InferenceFulfilled",
        "type": "event",
        "inputs": [
            {"name": "requestId",  "type": "bytes32", "indexed": True},
            {"name": "requester",  "type": "address", "indexed": True},
            {"name": "result",     "type": "int256",  "indexed": False},
            {"name": "confidence", "type": "uint8",   "indexed": False},
            {"name": "ogTxHash",   "type": "string",  "indexed": False},
            {"name": "timestamp",  "type": "uint256", "indexed": False},
        ],
    },
    {
        "name": "fulfillInference",
        "type": "function",
        "inputs": [
            {"name": "requestId",  "type": "bytes32"},
            {"name": "result",     "type": "int256"},
            {"name": "confidence", "type": "uint8"},
            {"name": "ogTxHash",   "type": "string"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "failInference",
        "type": "function",
        "inputs": [
            {"name": "requestId", "type": "bytes32"},
            {"name": "reason",    "type": "string"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "getRequest",
        "type": "function",
        "inputs": [{"name": "requestId", "type": "bytes32"}],
        "outputs": [
            {
                "components": [
                    {"name": "requestId",    "type": "bytes32"},
                    {"name": "requester",    "type": "address"},
                    {"name": "modelCid",     "type": "string"},
                    {"name": "features",     "type": "int256[]"},
                    {"name": "modelProfile", "type": "string"},
                    {"name": "status",       "type": "uint8"},
                    {"name": "result",       "type": "int256"},
                    {"name": "confidence",   "type": "uint8"},
                    {"name": "requestedAt",  "type": "uint256"},
                    {"name": "fulfilledAt",  "type": "uint256"},
                    {"name": "ogTxHash",     "type": "string"},
                ],
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
    },
    {
        "name": "getTotalRequests",
        "type": "function",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
    },
]

# ── Inference profiles ────────────────────────────────────────
# Maps model profile name → how to decode features + choose inference mode
INFERENCE_PROFILES = {
    "defi_risk": {
        "scale": FEATURE_SCALE,
        "mode":  og.InferenceMode.VANILLA,
        "tensor": "X",
    },
    "trading_signal": {
        "scale": FEATURE_SCALE,
        "mode":  og.InferenceMode.VANILLA,
        "tensor": "X",
    },
    "wallet_security": {
        "scale": FEATURE_SCALE,
        "mode":  og.InferenceMode.VANILLA,
        "tensor": "X",
    },
    "credit_score": {
        "scale": FEATURE_SCALE,
        "mode":  og.InferenceMode.VANILLA,
        "tensor": "X",
    },
    "nft_analysis": {
        "scale": FEATURE_SCALE,
        "mode":  og.InferenceMode.VANILLA,
        "tensor": "X",
    },
    "default": {
        "scale": FEATURE_SCALE,
        "mode":  og.InferenceMode.VANILLA,
        "tensor": "X",
    },
}


class OGOracleListener:
    """
    Listens for InferenceRequested events on the OGOracle contract,
    runs OpenGradient inference, and fulfills each request on-chain.
    """

    def __init__(self):
        # ── Web3 setup ────────────────────────────────────────
        self.w3 = Web3(Web3.HTTPProvider(OG_RPC_URL))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        if not self.w3.is_connected():
            log.error("Cannot connect to OG RPC: %s", OG_RPC_URL)
            sys.exit(1)

        log.info("Connected to OG Testnet — Chain %d", self.w3.eth.chain_id)

        # ── Account setup ─────────────────────────────────────
        if not PRIVATE_KEY:
            log.error("OG_PRIVATE_KEY not set in .env")
            sys.exit(1)

        self.account = self.w3.eth.account.from_key(PRIVATE_KEY)
        log.info("Fulfiller wallet: %s", self.account.address)

        # Check balance
        balance = self.w3.eth.get_balance(self.account.address)
        log.info("Wallet balance: %.6f ETH", self.w3.from_wei(balance, "ether"))

        if balance == 0:
            log.warning("⚠  Zero balance! Get testnet ETH from https://faucet.opengradient.ai")

        # ── Contract setup ────────────────────────────────────
        if not CONTRACT_ADDRESS:
            log.error("CONTRACT_ADDRESS not set in .env")
            sys.exit(1)

        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=ORACLE_ABI,
        )
        log.info("Oracle contract: %s", CONTRACT_ADDRESS)

        # ── OpenGradient Alpha client ─────────────────────────
        self.og_alpha = og.Alpha(private_key=PRIVATE_KEY)
        log.info("OpenGradient Alpha client initialised")

        # Track last processed block
        self.last_block = self.w3.eth.block_number
        log.info("Starting from block #%d", self.last_block)

        # Track processed request IDs to avoid double-processing
        self.processed = set()

        # Stats
        self.total_processed = 0
        self.total_fulfilled  = 0
        self.total_failed     = 0

    # ── Decode features ───────────────────────────────────────

    def decode_features(self, raw_features: list[int], profile: str) -> list[float]:
        """
        Convert on-chain int256 features back to floats.
        On-chain: 1.5 → 15000 (scaled by FEATURE_SCALE)
        """
        cfg   = INFERENCE_PROFILES.get(profile, INFERENCE_PROFILES["default"])
        scale = cfg["scale"]
        return [float(f) / scale for f in raw_features]

    # ── Run OpenGradient inference ────────────────────────────

    def run_inference(
        self,
        model_cid: str,
        features:  list[float],
        profile:   str,
    ) -> tuple[float, int, str]:
        """
        Run model inference through OpenGradient.

        Returns:
            (result_float, confidence_int, og_tx_hash)
        """
        cfg        = INFERENCE_PROFILES.get(profile, INFERENCE_PROFILES["default"])
        tensor_name= cfg["tensor"]
        mode       = cfg["mode"]

        log.info("  Running OG inference — model: %s", model_cid[:20])
        log.info("  Features: %s", features)

        result = self.og_alpha.infer(
            model_cid=model_cid,
            model_input={tensor_name: [features]},
            inference_mode=mode,
        )

        # Extract scalar output
        output = result.model_output
        if isinstance(output, dict):
            values = list(output.values())[0]
            if hasattr(values, "flatten"):
                scalar = float(values.flatten()[0])
            elif isinstance(values, (list, tuple)):
                scalar = float(values[0][0] if isinstance(values[0], (list, tuple)) else values[0])
            else:
                scalar = float(values)
        else:
            scalar = float(output)

        # Clamp to [0, 1] for classifiers, or [0, 100] for regression
        scalar = max(0.0, min(100.0, scalar))

        # Confidence: based on distance from 0.5 (for binary classifiers)
        if scalar <= 1.0:
            confidence = int(abs(scalar - 0.5) * 200)  # 0→100
        else:
            confidence = 85  # regression models

        og_tx_hash = getattr(result, "tx_hash", "") or ""
        log.info("  Output: %.6f | Confidence: %d | OG TX: %s",
                 scalar, confidence, og_tx_hash[:20] if og_tx_hash else "none")

        return scalar, confidence, og_tx_hash

    # ── Fulfill on-chain ──────────────────────────────────────

    def fulfill_on_chain(
        self,
        request_id: bytes,
        result:     float,
        confidence: int,
        og_tx_hash: str,
    ) -> str:
        """
        Call fulfillInference() on the oracle contract.
        Returns the fulfillment transaction hash.
        """
        # Scale result back to int for on-chain storage
        result_scaled = int(result * RESULT_SCALE)
        confidence_u8 = max(0, min(255, confidence))

        nonce = self.w3.eth.get_transaction_count(self.account.address)
        gas_price = self.w3.eth.gas_price

        tx = self.contract.functions.fulfillInference(
            request_id,
            result_scaled,
            confidence_u8,
            og_tx_hash,
        ).build_transaction({
            "chainId":  OG_CHAIN_ID,
            "from":     self.account.address,
            "nonce":    nonce,
            "gas":      200_000,
            "gasPrice": gas_price,
        })

        signed  = self.w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] == 1:
            return self.w3.to_hex(tx_hash)
        else:
            raise RuntimeError(f"fulfillInference tx reverted: {self.w3.to_hex(tx_hash)}")

    def fail_on_chain(self, request_id: bytes, reason: str) -> str:
        """Mark a request as failed on-chain."""
        nonce = self.w3.eth.get_transaction_count(self.account.address)
        tx = self.contract.functions.failInference(
            request_id, reason
        ).build_transaction({
            "chainId":  OG_CHAIN_ID,
            "from":     self.account.address,
            "nonce":    nonce,
            "gas":      100_000,
            "gasPrice": self.w3.eth.gas_price,
        })
        signed  = self.w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        return self.w3.to_hex(tx_hash)

    # ── Process a single event ────────────────────────────────

    def process_event(self, event) -> None:
        args       = event["args"]
        request_id = args["requestId"]
        hex_id     = request_id.hex()

        if hex_id in self.processed:
            return

        self.processed.add(hex_id)
        self.total_processed += 1

        log.info("─" * 60)
        log.info("📥 New inference request #%d", self.total_processed)
        log.info("   Request ID : 0x%s", hex_id[:16])
        log.info("   Requester  : %s", args["requester"])
        log.info("   Model CID  : %s", args["modelCid"])
        log.info("   Profile    : %s", args["modelProfile"])
        log.info("   Features   : %s (raw)", list(args["features"]))

        try:
            # 1. Decode features from int256 to float
            features = self.decode_features(
                list(args["features"]),
                args["modelProfile"],
            )
            log.info("   Features   : %s (decoded)", features)

            # 2. Run OpenGradient inference
            result, confidence, og_tx_hash = self.run_inference(
                model_cid=args["modelCid"],
                features=features,
                profile=args["modelProfile"],
            )

            # 3. Fulfill on-chain
            log.info("  Writing result to chain...")
            fulfill_tx = self.fulfill_on_chain(
                request_id, result, confidence, og_tx_hash
            )

            self.total_fulfilled += 1
            log.info("✅ Fulfilled!")
            log.info("   Result     : %.6f", result)
            log.info("   Confidence : %d%%", confidence)
            log.info("   OG Proof   : %s", og_tx_hash or "N/A")
            log.info("   Fulfill TX : %s", fulfill_tx)
            log.info("   Explorer   : https://explorer.opengradient.ai/tx/%s", fulfill_tx)

        except Exception as exc:
            self.total_failed += 1
            reason = str(exc)[:120]
            log.error("❌ Inference failed: %s", reason)
            try:
                self.fail_on_chain(request_id, reason)
            except Exception as fail_exc:
                log.error("   Could not mark as failed: %s", fail_exc)

    # ── Main polling loop ─────────────────────────────────────

    def run(self) -> None:
        log.info("=" * 60)
        log.info("  OG Oracle Listener — RUNNING")
        log.info("  Polling every %ds for InferenceRequested events", POLL_INTERVAL_SEC)
        log.info("=" * 60)

        # Check total requests already on contract
        try:
            total = self.contract.functions.getTotalRequests().call()
            log.info("  Contract has %d existing requests", total)
        except Exception:
            pass

        while True:
            try:
                current_block = self.w3.eth.block_number

                if current_block > self.last_block:
                    events = self.contract.events.InferenceRequested.get_logs(
                        from_block=self.last_block + 1,
                        to_block=current_block,
                    )

                    if events:
                        log.info("📦 Block #%d — %d new event(s)", current_block, len(events))
                        for event in events:
                            self.process_event(event)
                    else:
                        log.debug("Block #%d — no new events", current_block)

                    self.last_block = current_block

                    # Print stats every 20 blocks
                    if current_block % 20 == 0:
                        log.info("📊 Stats: processed=%d fulfilled=%d failed=%d",
                                 self.total_processed, self.total_fulfilled, self.total_failed)

            except KeyboardInterrupt:
                log.info("\n⛔ Listener stopped by user")
                log.info("   Final stats: processed=%d fulfilled=%d failed=%d",
                         self.total_processed, self.total_fulfilled, self.total_failed)
                break
            except Exception as exc:
                log.error("Poll error: %s", exc)
                time.sleep(5)  # back off on error

            time.sleep(POLL_INTERVAL_SEC)


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════╗
║         OpenGradient On-Chain AI Oracle              ║
║         Oracle Listener v1.0.0                       ║
╚══════════════════════════════════════════════════════╝
    """)

    listener = OGOracleListener()
    listener.run()
