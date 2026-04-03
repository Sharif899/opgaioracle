// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title OGOracle
 * @author OpenGradient Community
 * @notice On-chain AI oracle that bridges Solidity smart contracts
 *         with OpenGradient TEE-verified ML inference.
 *
 * HOW IT WORKS:
 *  1. Any contract calls requestInference() with a model CID + input features
 *  2. The contract emits an InferenceRequested event and stores the request
 *  3. A Python listener (oracle_listener.py) picks up the event
 *  4. The listener runs the model through OpenGradient and calls fulfillInference()
 *  5. The contract stores the result and emits InferenceFulfilled
 *  6. The requesting contract receives the result via callback
 *
 * DEPLOYED ON: OpenGradient Testnet (Chain ID: 10740)
 * RPC: https://ogevmdevnet.opengradient.ai
 */

interface IOGOracleConsumer {
    /**
     * @notice Callback called by the oracle when inference is ready
     * @param requestId  The unique request ID
     * @param result     The model output (scaled by 1e6 to preserve decimals)
     * @param confidence Confidence score 0–100
     */
    function onInferenceFulfilled(
        bytes32 requestId,
        int256  result,
        uint8   confidence
    ) external;
}

contract OGOracle {

    // ── State ────────────────────────────────────────────────────

    address public owner;
    address public fulfiller;          // authorised oracle listener address
    uint256 public requestCounter;
    uint256 public constant RESULT_SCALE = 1e6; // results multiplied by 1e6 on-chain

    enum RequestStatus { Pending, Fulfilled, Failed }

    struct InferenceRequest {
        bytes32    requestId;
        address    requester;          // contract that requested
        string     modelCid;           // OpenGradient Hub model CID
        int256[]   features;           // input feature vector
        string     modelProfile;       // e.g. "defi_risk", "trading_signal"
        RequestStatus status;
        int256     result;             // scaled by RESULT_SCALE
        uint8      confidence;         // 0–100
        uint256    requestedAt;
        uint256    fulfilledAt;
        string     ogTxHash;           // OG network settlement tx
    }

    mapping(bytes32 => InferenceRequest) public requests;
    bytes32[] public allRequestIds;

    // per-consumer request history
    mapping(address => bytes32[]) public consumerRequests;

    // ── Events ────────────────────────────────────────────────────

    event InferenceRequested(
        bytes32 indexed requestId,
        address indexed requester,
        string          modelCid,
        string          modelProfile,
        int256[]        features,
        uint256         timestamp
    );

    event InferenceFulfilled(
        bytes32 indexed requestId,
        address indexed requester,
        int256          result,
        uint8           confidence,
        string          ogTxHash,
        uint256         timestamp
    );

    event InferenceFailed(
        bytes32 indexed requestId,
        address indexed requester,
        string          reason,
        uint256         timestamp
    );

    event FulfillerUpdated(address indexed oldFulfiller, address indexed newFulfiller);

    // ── Errors ────────────────────────────────────────────────────

    error OnlyOwner();
    error OnlyFulfiller();
    error RequestNotFound();
    error RequestAlreadyFulfilled();
    error InvalidModelCid();
    error EmptyFeatures();
    error TooManyFeatures();
    error CallbackFailed();

    // ── Modifiers ─────────────────────────────────────────────────

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    modifier onlyFulfiller() {
        if (msg.sender != fulfiller) revert OnlyFulfiller();
        _;
    }

    // ── Constructor ───────────────────────────────────────────────

    constructor(address _fulfiller) {
        owner     = msg.sender;
        fulfiller = _fulfiller;
        emit FulfillerUpdated(address(0), _fulfiller);
    }

    // ── Core: Request Inference ───────────────────────────────────

    /**
     * @notice Request an AI inference from OpenGradient.
     *         The caller must be a contract implementing IOGOracleConsumer.
     *
     * @param modelCid     Blob CID of the ONNX model on OpenGradient Hub
     * @param features     Input feature vector (integers, scaled as needed)
     * @param modelProfile Profile name for input generation ("defi_risk" etc.)
     * @return requestId   Unique ID for this inference request
     *
     * @dev Features are stored as int256 to support negative values.
     *      Multiply decimals by 1e4 before passing (e.g. 1.5 → 15000).
     *
     * Example (DeFi liquidation risk):
     *   features = [15000, 7000, 2500, 11000, 30000]
     *   means    = [1.5,   0.7,  0.25, 1.1,   30.0] (collateral,borrow,vol,health,days)
     */
    function requestInference(
        string   calldata modelCid,
        int256[] calldata features,
        string   calldata modelProfile
    )
        external
        returns (bytes32 requestId)
    {
        if (bytes(modelCid).length == 0)   revert InvalidModelCid();
        if (features.length == 0)           revert EmptyFeatures();
        if (features.length > 32)           revert TooManyFeatures();

        // Generate unique request ID
        requestId = keccak256(abi.encodePacked(
            msg.sender,
            modelCid,
            features,
            block.timestamp,
            ++requestCounter
        ));

        // Store request
        requests[requestId] = InferenceRequest({
            requestId:    requestId,
            requester:    msg.sender,
            modelCid:     modelCid,
            features:     features,
            modelProfile: modelProfile,
            status:       RequestStatus.Pending,
            result:       0,
            confidence:   0,
            requestedAt:  block.timestamp,
            fulfilledAt:  0,
            ogTxHash:     ""
        });

        allRequestIds.push(requestId);
        consumerRequests[msg.sender].push(requestId);

        emit InferenceRequested(
            requestId,
            msg.sender,
            modelCid,
            modelProfile,
            features,
            block.timestamp
        );

        return requestId;
    }

    // ── Core: Fulfill Inference ───────────────────────────────────

    /**
     * @notice Called by the oracle listener to deliver an inference result.
     *         Only the authorised fulfiller address can call this.
     *
     * @param requestId  The request being fulfilled
     * @param result     Model output × RESULT_SCALE (e.g. 0.87 → 870000)
     * @param confidence Confidence score 0–100
     * @param ogTxHash   OpenGradient settlement transaction hash
     *
     * @dev After storing the result, calls back the requesting contract
     *      via IOGOracleConsumer.onInferenceFulfilled(). If the callback
     *      reverts, the result is still stored — the requester can poll.
     */
    function fulfillInference(
        bytes32 requestId,
        int256  result,
        uint8   confidence,
        string  calldata ogTxHash
    )
        external
        onlyFulfiller
    {
        InferenceRequest storage req = requests[requestId];
        if (req.requestedAt == 0)                        revert RequestNotFound();
        if (req.status != RequestStatus.Pending)         revert RequestAlreadyFulfilled();

        req.status      = RequestStatus.Fulfilled;
        req.result      = result;
        req.confidence  = confidence;
        req.ogTxHash    = ogTxHash;
        req.fulfilledAt = block.timestamp;

        emit InferenceFulfilled(
            requestId,
            req.requester,
            result,
            confidence,
            ogTxHash,
            block.timestamp
        );

        // Callback to requesting contract — non-reverting
        if (req.requester.code.length > 0) {
            try IOGOracleConsumer(req.requester).onInferenceFulfilled(
                requestId, result, confidence
            ) { } catch {
                // Callback failed — result is still stored, requester can poll
                emit InferenceFailed(requestId, req.requester, "Callback reverted", block.timestamp);
            }
        }
    }

    /**
     * @notice Mark a request as failed (e.g. invalid model, network error).
     */
    function failInference(bytes32 requestId, string calldata reason)
        external
        onlyFulfiller
    {
        InferenceRequest storage req = requests[requestId];
        if (req.requestedAt == 0)            revert RequestNotFound();
        if (req.status != RequestStatus.Pending) revert RequestAlreadyFulfilled();

        req.status      = RequestStatus.Failed;
        req.fulfilledAt = block.timestamp;

        emit InferenceFailed(requestId, req.requester, reason, block.timestamp);
    }

    // ── Views ─────────────────────────────────────────────────────

    function getRequest(bytes32 requestId)
        external view
        returns (InferenceRequest memory)
    {
        return requests[requestId];
    }

    function getRequestFeatures(bytes32 requestId)
        external view
        returns (int256[] memory)
    {
        return requests[requestId].features;
    }

    function getConsumerRequests(address consumer)
        external view
        returns (bytes32[] memory)
    {
        return consumerRequests[consumer];
    }

    function getTotalRequests() external view returns (uint256) {
        return allRequestIds.length;
    }

    function getLatestRequestId() external view returns (bytes32) {
        if (allRequestIds.length == 0) return bytes32(0);
        return allRequestIds[allRequestIds.length - 1];
    }

    /**
     * @notice Get the decoded result (divides by RESULT_SCALE).
     *         Returns a scaled integer — divide by 1e6 off-chain for the float.
     */
    function getResultScaled(bytes32 requestId)
        external view
        returns (int256 result, uint8 confidence, bool fulfilled)
    {
        InferenceRequest storage req = requests[requestId];
        return (req.result, req.confidence, req.status == RequestStatus.Fulfilled);
    }

    // ── Admin ─────────────────────────────────────────────────────

    function setFulfiller(address newFulfiller) external onlyOwner {
        emit FulfillerUpdated(fulfiller, newFulfiller);
        fulfiller = newFulfiller;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        owner = newOwner;
    }
}


// ─────────────────────────────────────────────────────────────────
// EXAMPLE CONSUMER CONTRACT
// Shows how any DeFi protocol would use the oracle
// ─────────────────────────────────────────────────────────────────

/**
 * @title DeFiRiskManager
 * @notice Example contract showing how to consume the OGOracle.
 *         A lending protocol could use this to get AI-verified
 *         liquidation risk scores before executing liquidations.
 */
contract DeFiRiskManager is IOGOracleConsumer {

    OGOracle public oracle;
    address  public owner;

    // Model CID on OpenGradient Hub
    string public constant LIQUIDATION_MODEL =
        "defi_liquidation_risk";

    // Risk decisions keyed by requestId
    struct RiskDecision {
        bytes32 requestId;
        address protocol;
        int256  riskScore;      // scaled by 1e6
        uint8   confidence;
        bool    shouldLiquidate;
        uint256 decidedAt;
    }

    mapping(bytes32 => RiskDecision) public decisions;
    mapping(address => bytes32)      public latestDecision;

    event RiskAssessed(
        bytes32 indexed requestId,
        address indexed protocol,
        int256          riskScore,
        bool            shouldLiquidate
    );

    modifier onlyOwner() { require(msg.sender == owner, "Not owner"); _; }

    constructor(address _oracle) {
        oracle = OGOracle(_oracle);
        owner  = msg.sender;
    }

    /**
     * @notice Request a liquidation risk assessment for a borrowing position.
     *
     * @param protocol        Address of the lending protocol position
     * @param collateralRatio Collateral / debt × 10000 (e.g. 1.5x → 15000)
     * @param borrowedRatio   Borrowed / max × 10000 (e.g. 70% → 7000)
     * @param assetVolatility Volatility × 10000 (e.g. 25% → 2500)
     * @param healthFactor    Health factor × 10000 (e.g. 1.1 → 11000)
     * @param daysSinceDeposit Days as integer
     */
    function assessLiquidationRisk(
        address protocol,
        int256  collateralRatio,
        int256  borrowedRatio,
        int256  assetVolatility,
        int256  healthFactor,
        int256  daysSinceDeposit
    )
        external
        onlyOwner
        returns (bytes32 requestId)
    {
        int256[] memory features = new int256[](5);
        features[0] = collateralRatio;
        features[1] = borrowedRatio;
        features[2] = assetVolatility;
        features[3] = healthFactor;
        features[4] = daysSinceDeposit;

        requestId = oracle.requestInference(
            LIQUIDATION_MODEL,
            features,
            "defi_risk"
        );

        // Store pending decision
        decisions[requestId] = RiskDecision({
            requestId:       requestId,
            protocol:        protocol,
            riskScore:       0,
            confidence:      0,
            shouldLiquidate: false,
            decidedAt:       0
        });

        latestDecision[protocol] = requestId;
        return requestId;
    }

    /**
     * @notice OGOracle calls this when inference is ready.
     *         result > 500000 (0.5 × 1e6) = HIGH RISK → recommend liquidation
     */
    function onInferenceFulfilled(
        bytes32 requestId,
        int256  result,
        uint8   confidence
    )
        external
        override
    {
        require(msg.sender == address(oracle), "Only oracle");

        RiskDecision storage decision = decisions[requestId];
        decision.riskScore       = result;
        decision.confidence      = confidence;
        decision.shouldLiquidate = result > int256(500000); // > 0.5 × 1e6
        decision.decidedAt       = block.timestamp;

        emit RiskAssessed(
            requestId,
            decision.protocol,
            result,
            decision.shouldLiquidate
        );
    }

    function getLatestRisk(address protocol)
        external view
        returns (int256 riskScore, bool shouldLiquidate, uint8 confidence)
    {
        bytes32 rid = latestDecision[protocol];
        RiskDecision storage d = decisions[rid];
        return (d.riskScore, d.shouldLiquidate, d.confidence);
    }
}
