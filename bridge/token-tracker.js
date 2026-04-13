/**
 * Per-User Token Tracker — In-memory accumulator with CloudWatch metric publishing.
 *
 * Tracks input/output token counts per actorId across all Bedrock invocations.
 * Publishes aggregated metrics to CloudWatch every 60s in batch.
 *
 * Usage:
 *   const tracker = require("./token-tracker");
 *   tracker.record("telegram:123", "telegram", "global.anthropic.claude-opus-4-6-v1", 1200, 340, false);
 *   tracker.getStats("telegram:123");  // per-user stats
 *   tracker.getStats();                // all users
 *   tracker.startPublisher();          // begin CloudWatch publishing
 *   tracker.stopPublisher();           // flush + stop
 */

const METRICS_NAMESPACE = "OpenClaw/TokenUsage";
const PUBLISH_INTERVAL_MS = 60_000;
const MAX_METRICS_PER_CALL = 25; // CloudWatch limit is 1000, but keep batches small

// Per-user cumulative stats (survive across publish cycles, reset on container restart)
const userStats = new Map();

// Pending metric data points for next CloudWatch publish cycle
let pendingRecords = [];

// Publisher timer
let publishTimer = null;

// Lazily initialized CloudWatch client wrapper
let _cwClient = null;
function getCloudWatchClient() {
  if (!_cwClient) {
    const { CloudWatchClient, PutMetricDataCommand } = require("@aws-sdk/client-cloudwatch");
    const rawClient = new CloudWatchClient({ region: process.env.AWS_REGION || "us-west-2" });
    _cwClient = {
      async putMetricData(params) {
        return rawClient.send(new PutMetricDataCommand(params));
      },
    };
  }
  return _cwClient;
}

// Cost estimation per model (USD per 1M tokens)
const MODEL_PRICING = {
  // Claude Opus 4.6 (cross-region)
  "global.anthropic.claude-opus-4-6-v1": { input: 15, output: 75 },
  // Claude Sonnet 4.6
  "global.anthropic.claude-sonnet-4-6-v1": { input: 3, output: 15 },
  // Claude Haiku 4.5
  "global.anthropic.claude-haiku-4-5-v1": { input: 0.8, output: 4 },
  // Fallback
  default: { input: 15, output: 75 },
};

function estimateCost(modelId, inputTokens, outputTokens) {
  // Match model pricing by prefix (cross-region IDs may have version suffixes)
  let pricing = MODEL_PRICING.default;
  for (const [key, p] of Object.entries(MODEL_PRICING)) {
    if (key !== "default" && modelId && modelId.startsWith(key)) {
      pricing = p;
      break;
    }
  }
  return (inputTokens * pricing.input + outputTokens * pricing.output) / 1_000_000;
}

/**
 * Record a Bedrock invocation's token usage.
 *
 * @param {string} actorId - User identity (e.g., "telegram:123456")
 * @param {string} channel - Channel name (e.g., "telegram")
 * @param {string} modelId - Bedrock model ID used
 * @param {number} inputTokens - Input token count
 * @param {number} outputTokens - Output token count
 * @param {boolean} isSubagent - Whether this was a subagent request
 */
function record(actorId, channel, modelId, inputTokens, outputTokens, isSubagent) {
  if (!inputTokens && !outputTokens) return;

  // Update cumulative per-user stats
  const stats = userStats.get(actorId) || {
    input: 0, output: 0, requests: 0,
    subagentInput: 0, subagentOutput: 0, subagentRequests: 0,
    channel: channel || "unknown",
    lastActivity: 0,
  };
  stats.input += inputTokens;
  stats.output += outputTokens;
  stats.requests++;
  stats.channel = channel || stats.channel;
  stats.lastActivity = Date.now();
  if (isSubagent) {
    stats.subagentInput += inputTokens;
    stats.subagentOutput += outputTokens;
    stats.subagentRequests++;
  }
  userStats.set(actorId, stats);

  // Queue for CloudWatch publishing
  pendingRecords.push({
    actorId,
    channel: channel || "unknown",
    modelId: modelId || "unknown",
    inputTokens,
    outputTokens,
    isSubagent: !!isSubagent,
    timestamp: new Date(),
  });
}

/**
 * Get cumulative stats for one user or all users.
 *
 * @param {string} [actorId] - Specific user (omit for all)
 * @returns {object} Stats object
 */
function getStats(actorId) {
  if (actorId) {
    return userStats.get(actorId) || null;
  }
  // Aggregate all users
  let totalInput = 0, totalOutput = 0, totalRequests = 0;
  const users = {};
  for (const [id, s] of userStats) {
    totalInput += s.input;
    totalOutput += s.output;
    totalRequests += s.requests;
    users[id] = { ...s };
  }
  return { totalInput, totalOutput, totalRequests, users };
}

/**
 * Build CloudWatch metric data from pending records and publish in batch.
 * Aggregates per-actorId within the batch window, plus a global (no-dimension) metric.
 */
async function flush() {
  if (pendingRecords.length === 0) return;

  const records = pendingRecords;
  pendingRecords = [];

  // Aggregate by actorId
  const byUser = new Map();
  let globalInput = 0, globalOutput = 0, globalRequests = 0, globalCost = 0;

  for (const r of records) {
    const key = r.actorId;
    const agg = byUser.get(key) || { input: 0, output: 0, requests: 0, cost: 0, channel: r.channel };
    agg.input += r.inputTokens;
    agg.output += r.outputTokens;
    agg.requests++;
    agg.cost += estimateCost(r.modelId, r.inputTokens, r.outputTokens);
    byUser.set(key, agg);

    globalInput += r.inputTokens;
    globalOutput += r.outputTokens;
    globalRequests++;
    globalCost += estimateCost(r.modelId, r.inputTokens, r.outputTokens);
  }

  const now = new Date();
  const metricData = [];

  // Global metrics (no dimensions — matches existing dashboard expectations)
  metricData.push(
    { MetricName: "InputTokens", Value: globalInput, Unit: "Count", Timestamp: now },
    { MetricName: "OutputTokens", Value: globalOutput, Unit: "Count", Timestamp: now },
    { MetricName: "TotalTokens", Value: globalInput + globalOutput, Unit: "Count", Timestamp: now },
    { MetricName: "EstimatedCostUSD", Value: globalCost, Unit: "None", Timestamp: now },
    { MetricName: "InvocationCount", Value: globalRequests, Unit: "Count", Timestamp: now },
  );

  // Per-user metrics (with ActorId dimension)
  for (const [actorId, agg] of byUser) {
    const dims = [{ Name: "ActorId", Value: actorId }];
    metricData.push(
      { MetricName: "InputTokens", Dimensions: dims, Value: agg.input, Unit: "Count", Timestamp: now },
      { MetricName: "OutputTokens", Dimensions: dims, Value: agg.output, Unit: "Count", Timestamp: now },
      { MetricName: "TotalTokens", Dimensions: dims, Value: agg.input + agg.output, Unit: "Count", Timestamp: now },
      { MetricName: "EstimatedCostUSD", Dimensions: dims, Value: agg.cost, Unit: "None", Timestamp: now },
      { MetricName: "RequestCount", Dimensions: dims, Value: agg.requests, Unit: "Count", Timestamp: now },
    );
  }

  // Publish in batches
  const client = getCloudWatchClient();

  for (let i = 0; i < metricData.length; i += MAX_METRICS_PER_CALL) {
    const batch = metricData.slice(i, i + MAX_METRICS_PER_CALL);
    try {
      await client.putMetricData({
        Namespace: METRICS_NAMESPACE,
        MetricData: batch,
      });
    } catch (err) {
      console.error(`[token-tracker] CloudWatch publish failed: ${err.message}`);
    }
  }

  console.log(
    `[token-tracker] Published ${metricData.length} metrics (${records.length} records, ${byUser.size} users, ` +
    `${globalInput}in/${globalOutput}out tokens, $${globalCost.toFixed(4)})`,
  );
}

/**
 * Start the periodic CloudWatch publisher.
 * @param {number} [intervalMs=60000] - Publish interval in milliseconds
 */
function startPublisher(intervalMs) {
  if (publishTimer) return;
  const interval = intervalMs || PUBLISH_INTERVAL_MS;
  publishTimer = setInterval(() => {
    flush().catch(err => {
      console.error(`[token-tracker] Periodic flush error: ${err.message}`);
    });
  }, interval);
  // Don't prevent process exit
  if (publishTimer.unref) publishTimer.unref();
  console.log(`[token-tracker] Publisher started (interval=${interval}ms, namespace=${METRICS_NAMESPACE})`);
}

/**
 * Stop the publisher and do a final flush.
 */
async function stopPublisher() {
  if (publishTimer) {
    clearInterval(publishTimer);
    publishTimer = null;
  }
  await flush();
  console.log("[token-tracker] Publisher stopped, final flush complete");
}

// Expose for testing
function _reset() {
  userStats.clear();
  pendingRecords = [];
  _cwClient = null;
}

function _setClient(client) {
  _cwClient = client;
}

module.exports = {
  record,
  getStats,
  flush,
  startPublisher,
  stopPublisher,
  estimateCost,
  _reset,
  _setClient,
  get _pendingRecords() { return pendingRecords; },
  get _userStats() { return userStats; },
  METRICS_NAMESPACE,
};
