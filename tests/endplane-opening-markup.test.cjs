"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const markup = require("../static/endplane-opening-markup.js");

test("opening markup keeps a stable side-specific id and rejects malformed points", () => {
  const value = markup.normalize("left", {
    seed: [1, 2, 3],
    outline: [[0, 0, 0], [1, 1, 1], ["bad", 0, 0], [2, 2, 2]],
  });
  assert.equal(value.opening_id, "left_nostril_opening");
  assert.deepEqual(value.seed_lps_mm, [1, 2, 3]);
  assert.deepEqual(value.outline_lps_mm, [[0, 0, 0], [1, 1, 1], [2, 2, 2]]);
  assert.equal(value.status, "outline_draft");
  assert.equal(value.persisted, false);
});

test("prototype summary never claims a formal opening or conversion", () => {
  const value = markup.withOutline(markup.withSeed(markup.empty("right"), [3, 2, 1]), [
    [0, 0, 0], [1, 0, 0], [1, 1, 0],
  ]);
  const summary = markup.summary(value);
  assert.equal(summary.id, "right_nostril_opening");
  assert.match(summary.status, /草稿/);
  assert.equal(summary.canConvert, false);
  assert.equal(value.source, "three_d_model_draft");
});

test("empty markup is safe for all supported endpoint sides", () => {
  for (const side of ["left", "right", "posterior", "anterior", "unknown"]) {
    const value = markup.empty(side);
    assert.equal(value.seed_lps_mm, null);
    assert.deepEqual(value.outline_lps_mm, []);
    assert.equal(value.status, "empty");
  }
});

test("single-side anterior draft keeps its own opening id and reports invalid selection", () => {
  const value = markup.normalize("anterior", {
    seed_lps_mm: [1, 2, 3],
    quality_errors: [{ code: "no_faces", message: "圈选没有可见面" }],
  });
  assert.equal(value.opening_id, "anterior_nostril_opening");
  assert.equal(value.status, "patch_invalid");
});

const screenPolygon = [[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]];

function cube(x = 0) {
  const top = [[x - 1, -1, 1], [x + 1, -1, 1],
    [x + 1, 1, 1], [x - 1, 1, 1]];
  const bottom = top.map(([px, py]) => [px, py, -1]);
  const triangles = [[top[0], top[1], top[2]], [top[0], top[2], top[3]],
    [bottom[0], bottom[2], bottom[1]], [bottom[0], bottom[3], bottom[2]]];
  for (let i = 0; i < 4; i += 1) {
    const next = (i + 1) % 4;
    triangles.push([top[i], bottom[i], bottom[next]],
      [top[i], bottom[next], top[next]]);
  }
  return triangles;
}

function cylinder() {
  const triangles = [], sides = 8;
  const ring = (i, z) => [Math.cos(2 * Math.PI * i / sides),
    Math.sin(2 * Math.PI * i / sides), z];
  for (let i = 0; i < sides; i += 1) {
    const next = (i + 1) % sides;
    triangles.push([ring(i, 1), ring(i, 0), ring(next, 0)],
      [ring(i, 1), ring(next, 0), ring(next, 1)]);
  }
  const upperCount = triangles.length;
  for (let i = 0; i < sides; i += 1) {
    const next = (i + 1) % sides;
    triangles.push([ring(i, 0), ring(i, -1), ring(next, -1)],
      [ring(i, 0), ring(next, -1), ring(next, 0)],
      [[0, 0, 1], ring(next, 1), ring(i, 1)],
      [[0, 0, -1], ring(i, -1), ring(next, -1)]);
  }
  return { triangles, upperCount };
}

function meshArgs(triangles, selectedIds) {
  const selected = new Set(selectedIds);
  return {
    positions: new Float32Array(triangles.flat(2)),
    projectedPoints: triangles.map((_, id) => selected.has(id) ? [0, 0] : [20, 20]),
    polygon: screenPolygon,
  };
}

test("closed local patch returns original STL face IDs and true boundary geometry", () => {
  const result = markup.buildPortPatch({ ...meshArgs(cube(), [0, 1]), seedFaceId: 0 });
  assert.equal(result.ok, true);
  assert.deepEqual(result.selected_face_ids, [0, 1]);
  assert.equal(result.seed_face_id, 0);
  assert.equal(result.boundary_loop_lps_mm.length, 5);
  assert.deepEqual(result.boundary_loop_lps_mm[0], result.boundary_loop_lps_mm.at(-1));
  assert.ok(Math.abs(result.area_mm2 - 4) < 1e-6);
  assert.deepEqual(result.centroid_lps_mm, [0, 0, 1]);
  assert.deepEqual(result.normal_lps, [0, 0, 1]);
  assert.deepEqual(result.plane.point_lps_mm, result.centroid_lps_mm);
  const normalized = markup.normalize("left", result);
  assert.deepEqual(normalized.selected_face_ids, [0, 1]);
  assert.deepEqual(normalized.boundary_loop_lps_mm, result.boundary_loop_lps_mm);
  assert.equal(normalized.area_mm2, 4);
  assert.equal(normalized.status, "patch_draft");
  assert.match(markup.summary(normalized).status, /待校验/);
  assert.equal(markup.summary(normalized).canConvert, false);
});

test("a seed picks one connected patch without silently including another", () => {
  const input = meshArgs([...cube(), ...cube(6)], [0, 1, 12, 13]);
  const result = markup.buildPortPatch({ ...input, seedFaceId: 0 });
  assert.equal(result.ok, true);
  assert.deepEqual(result.selected_face_ids, [0, 1]);
  assert.equal(result.warnings[0].code, "ignored_disconnected_regions");
  const ambiguous = markup.buildPortPatch(input);
  assert.equal(ambiguous.ok, false);
  assert.equal(ambiguous.quality_errors[0].code, "disconnected_regions");
});

test("open STL edge cannot masquerade as a valid inlet", () => {
  const triangle = [[[0, 0, 0], [1, 0, 0], [0, 1, 0]]];
  const result = markup.buildPortPatch({ ...meshArgs(triangle, [0]), seedFaceId: 0 });
  assert.equal(result.ok, false);
  assert.ok(result.quality_errors.some((item) => item.code === "open_surface_boundary"));
});

test("a connected annulus with two boundary loops is rejected", () => {
  const { triangles, upperCount } = cylinder();
  const selected = Array.from({ length: upperCount }, (_, id) => id);
  const result = markup.buildPortPatch({ ...meshArgs(triangles, selected), seedFaceId: 0 });
  assert.equal(result.ok, false);
  assert.ok(result.quality_errors.some((item) => item.code === "multiple_boundaries"));
});

test("empty screen selection returns no faces and no synthetic boundary", () => {
  const result = markup.buildPortPatch(meshArgs(cube(), []));
  assert.equal(result.ok, false);
  assert.deepEqual(result.selected_face_ids, []);
  assert.deepEqual(result.boundary_loop_lps_mm, []);
  assert.equal(result.quality_errors[0].code, "no_faces");
});

test("self-crossing lasso is rejected before selecting geometry", () => {
  const star = Array.from({ length: 5 }, (_, index) => {
    const angle = 2 * Math.PI * index / 5;
    return [3 * Math.cos(angle), 3 * Math.sin(angle)];
  });
  const result = markup.buildPortPatch({ ...meshArgs(cube(), [0, 1]),
    polygon: [0, 2, 4, 1, 3].map((index) => star[index]), seedFaceId: 0 });
  assert.equal(result.ok, false);
  assert.equal(result.quality_errors[0].code, "invalid_polygon");
});

test("nonplanar nostril lip cannot be passed as one flat CFD port", () => {
  const triangles = cube();
  triangles[0][2][2] = 10;
  const result = markup.buildPortPatch({ ...meshArgs(triangles, [0, 1]), seedFaceId: 0 });
  assert.equal(result.ok, false);
  assert.ok(result.max_plane_deviation_mm > 0.5);
  assert.ok(result.quality_errors.some((item) => item.code === "nonplanar_boundary"));
});

test("changing a seed clears stale selected mesh faces", () => {
  const patch = markup.normalize("left",
    markup.buildPortPatch({ ...meshArgs(cube(), [0, 1]), seedFaceId: 0 }));
  const changed = markup.withSeed(patch, [0, 0, 2]);
  assert.deepEqual(changed.selected_face_ids, []);
  assert.deepEqual(changed.boundary_loop_lps_mm, []);
  assert.deepEqual(changed.outline_lps_mm, []);
});

test("depth visibility excludes back faces before selecting a local port patch", () => {
  const triangles = cube();
  const projectedTriangles = triangles.map((triangle) =>
    triangle.map(([x, y, z]) => [x, y, -z / 5]));
  const projectedPoints = triangles.map((triangle) => [
    triangle.reduce((sum, point) => sum + point[0] / 3, 0),
    triangle.reduce((sum, point) => sum + point[1] / 3, 0),
  ]);
  const visible = markup.visibleFaceIds({ projectedTriangles, projectedPoints,
    polygon: screenPolygon });
  assert.deepEqual(visible.quality_errors, []);
  assert.deepEqual(visible.eligibleFaceIds, [0, 1]);
  const patch = markup.buildPortPatch({ ...meshArgs(triangles,
    Array.from({ length: triangles.length }, (_, id) => id)), projectedPoints,
    eligibleFaceIds: visible.eligibleFaceIds, seedFaceId: 0 });
  assert.equal(patch.ok, true);
  assert.deepEqual(patch.selected_face_ids, [0, 1]);
});

test("visibility uses original face IDs and nearest NDC depth at each screen point", () => {
  const projectedTriangles = [
    [[0, 0, -0.5], [2, 0, -0.5], [0, 2, -0.5]],
    [[0, 0, 0.5], [2, 0, 0.5], [0, 2, 0.5]],
  ];
  const visible = markup.visibleFaceIds({ projectedTriangles,
    projectedPoints: [[0.5, 0.5], [0.5, 0.5]], polygon: screenPolygon });
  assert.deepEqual(visible.eligibleFaceIds, [0]);
});

test("visibility refuses triangles crossing the camera near plane", () => {
  const visible = markup.visibleFaceIds({ projectedTriangles: [
    [[0, 0, -1.2], [2, 0, 0], [0, 2, 0]],
  ], projectedPoints: [[0.5, 0.5]], polygon: screenPolygon });
  assert.deepEqual(visible.eligibleFaceIds, []);
  assert.equal(visible.quality_errors[0].code, "clip_required");
});
