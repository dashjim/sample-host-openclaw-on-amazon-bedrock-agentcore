const { describe, it, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const tracker = require("./token-tracker");

// Mock CloudWatch client — captures all putMetricData() calls
function createMockClient() {
  const sent = [];
  return {
    sent,
    async putMetricData(params) {
      sent.push({ input: params });
      return {};
    },
  };
}

describe("token-tracker", () => {
  let mockClient;

  beforeEach(() => {
    tracker._reset();
    mockClient = createMockClient();
    tracker._setClient(mockClient);
  });

  describe("record()", () => {
    it("should accumulate tokens for a single user", () => {
      tracker.record("telegram:123", "telegram", "model-a", 100, 50, false);
      tracker.record("telegram:123", "telegram", "model-a", 200, 80, false);

      const stats = tracker.getStats("telegram:123");
      assert.equal(stats.input, 300);
      assert.equal(stats.output, 130);
      assert.equal(stats.requests, 2);
      assert.equal(stats.subagentInput, 0);
    });

    it("should track subagent tokens separately", () => {
      tracker.record("telegram:123", "telegram", "model-a", 100, 50, false);
      tracker.record("telegram:123", "telegram", "model-sub", 80, 30, true);

      const stats = tracker.getStats("telegram:123");
      assert.equal(stats.input, 180);
      assert.equal(stats.output, 80);
      assert.equal(stats.requests, 2);
      assert.equal(stats.subagentInput, 80);
      assert.equal(stats.subagentOutput, 30);
      assert.equal(stats.subagentRequests, 1);
    });

    it("should track multiple users independently", () => {
      tracker.record("telegram:111", "telegram", "model-a", 100, 50, false);
      tracker.record("slack:222", "slack", "model-a", 200, 80, false);

      assert.equal(tracker.getStats("telegram:111").input, 100);
      assert.equal(tracker.getStats("slack:222").input, 200);
    });

    it("should skip records with zero tokens", () => {
      tracker.record("telegram:123", "telegram", "model-a", 0, 0, false);
      assert.equal(tracker.getStats("telegram:123"), null);
      assert.equal(tracker._pendingRecords.length, 0);
    });

    it("should queue pending records for CloudWatch", () => {
      tracker.record("telegram:123", "telegram", "model-a", 100, 50, false);
      assert.equal(tracker._pendingRecords.length, 1);
      assert.equal(tracker._pendingRecords[0].actorId, "telegram:123");
      assert.equal(tracker._pendingRecords[0].inputTokens, 100);
    });

    it("should update channel on subsequent requests", () => {
      tracker.record("telegram:123", "telegram", "model-a", 100, 50, false);
      tracker.record("telegram:123", "slack", "model-a", 100, 50, false);

      const stats = tracker.getStats("telegram:123");
      assert.equal(stats.channel, "slack");
    });
  });

  describe("getStats()", () => {
    it("should return null for unknown user", () => {
      assert.equal(tracker.getStats("unknown:user"), null);
    });

    it("should return aggregate stats when no actorId specified", () => {
      tracker.record("telegram:111", "telegram", "model-a", 100, 50, false);
      tracker.record("slack:222", "slack", "model-a", 200, 80, false);

      const all = tracker.getStats();
      assert.equal(all.totalInput, 300);
      assert.equal(all.totalOutput, 130);
      assert.equal(all.totalRequests, 2);
      assert.ok(all.users["telegram:111"]);
      assert.ok(all.users["slack:222"]);
    });
  });

  describe("estimateCost()", () => {
    it("should calculate cost for Claude Opus 4.6", () => {
      const cost = tracker.estimateCost("global.anthropic.claude-opus-4-6-v1", 1000, 500);
      // 1000 * $15/M + 500 * $75/M = $0.015 + $0.0375 = $0.0525
      assert.ok(Math.abs(cost - 0.0525) < 0.0001);
    });

    it("should use default pricing for unknown models", () => {
      const cost = tracker.estimateCost("unknown-model", 1000, 500);
      assert.ok(cost > 0);
    });

    it("should return 0 for zero tokens", () => {
      assert.equal(tracker.estimateCost("global.anthropic.claude-opus-4-6-v1", 0, 0), 0);
    });
  });

  describe("flush()", () => {
    it("should do nothing when no pending records", async () => {
      await tracker.flush();
      assert.equal(mockClient.sent.length, 0);
    });

    it("should publish global and per-user metrics", async () => {
      tracker.record("telegram:111", "telegram", "model-a", 100, 50, false);
      tracker.record("slack:222", "slack", "model-a", 200, 80, false);

      await tracker.flush();

      assert.ok(mockClient.sent.length > 0);
      const allMetrics = mockClient.sent.flatMap(cmd => cmd.input.MetricData);

      // Global metrics (no dimensions): 5
      const globalMetrics = allMetrics.filter(m => !m.Dimensions);
      assert.equal(globalMetrics.length, 5);

      const globalInput = globalMetrics.find(m => m.MetricName === "InputTokens");
      assert.equal(globalInput.Value, 300);

      const globalOutput = globalMetrics.find(m => m.MetricName === "OutputTokens");
      assert.equal(globalOutput.Value, 130);

      const globalTotal = globalMetrics.find(m => m.MetricName === "TotalTokens");
      assert.equal(globalTotal.Value, 430);

      // Per-user metrics: 5 metrics * 2 users = 10
      const userMetrics = allMetrics.filter(m => m.Dimensions);
      assert.equal(userMetrics.length, 10);

      const tgInput = userMetrics.find(
        m => m.MetricName === "InputTokens" && m.Dimensions[0].Value === "telegram:111"
      );
      assert.equal(tgInput.Value, 100);
    });

    it("should clear pending records after flush", async () => {
      tracker.record("telegram:111", "telegram", "model-a", 100, 50, false);
      await tracker.flush();
      assert.equal(tracker._pendingRecords.length, 0);
    });

    it("should use correct namespace", async () => {
      tracker.record("telegram:111", "telegram", "model-a", 100, 50, false);
      await tracker.flush();

      assert.equal(mockClient.sent[0].input.Namespace, "OpenClaw/TokenUsage");
    });

    it("should preserve cumulative stats across flushes", async () => {
      tracker.record("telegram:111", "telegram", "model-a", 100, 50, false);
      await tracker.flush();

      tracker.record("telegram:111", "telegram", "model-a", 200, 80, false);
      await tracker.flush();

      const stats = tracker.getStats("telegram:111");
      assert.equal(stats.input, 300);
      assert.equal(stats.output, 130);
    });
  });

  describe("startPublisher() / stopPublisher()", () => {
    it("should flush on stop", async () => {
      tracker.record("telegram:111", "telegram", "model-a", 100, 50, false);
      tracker.startPublisher(300000); // long interval
      await tracker.stopPublisher();

      assert.ok(mockClient.sent.length > 0);
      assert.equal(tracker._pendingRecords.length, 0);
    });
  });
});
