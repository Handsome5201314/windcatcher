"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const start = source.indexOf("function bilateralRetentionMetrics(");
assert.ok(start >= 0);
const definition = source.slice(start, source.indexOf("\n}", start) + 2);
const context = vm.createContext({});
vm.runInContext(definition, context);

test("three-opening retention excludes disconnected mask fragments from trim ratio", () => {
  const metrics = context.bilateralRetentionMetrics({
    retained_voxels: 900, left_deleted_voxels: 30, right_deleted_voxels: 50,
    posterior_deleted_voxels: 20, source_voxels: 1200,
  });
  assert.equal(metrics.retained, "90.0");
  assert.equal(metrics.deleted, "10.0");
  assert.equal(metrics.left, "3.0");
  assert.equal(metrics.right, "5.0");
  assert.equal(metrics.posterior, "2.0");
  assert.equal(metrics.excluded, 200);
});

test("retention cannot silently display inconsistent or missing voxel counts", () => {
  assert.equal(context.bilateralRetentionMetrics({ retained_voxels: 0, left_deleted_voxels: 0,
    right_deleted_voxels: 0, posterior_deleted_voxels: 0 }), null);
  assert.equal(context.bilateralRetentionMetrics({ retained_voxels: 900, left_deleted_voxels: 30,
    right_deleted_voxels: 50, posterior_deleted_voxels: 20, source_voxels: 900 }), null);
  assert.equal(context.bilateralRetentionMetrics({ retained_voxels: 900, left_deleted_voxels: -1,
    right_deleted_voxels: 50, posterior_deleted_voxels: 20 }), null);
});
