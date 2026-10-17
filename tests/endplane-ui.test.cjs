"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");

function functionSource(name) {
  const start = source.match(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.ok(start, `production function ${name} exists`);
  const end = source.indexOf("\n}", start.index);
  assert.notEqual(end, -1, `production function ${name} is complete`);
  return source.slice(start.index, end + 2);
}

function stateSource() {
  const start = source.indexOf("const state = {");
  const end = source.indexOf("\n};", start);
  assert.ok(start >= 0 && end > start, "production state exists");
  return source.slice(start, end + 3);
}

function plane(y, normalY = -1) {
  return { point_lps_mm: [1, y, 3], normal_lps: [0, normalY, 0] };
}

function assertJsonEqual(actual, expected) {
  assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

function harness() {
  const requests = [];
  const messages = [];
  const context = vm.createContext({
    window: {},
    endplaneContext: () => ({ caseId: "case-a", scope: "main_airway", version: 3, key: "case-a|main_airway|3" }),
    endplaneValidation: () => null,
    clearEndplanePreview: () => {},
    scheduleEndplanePreview: () => {},
    renderEndplaneUi: () => {},
    renderCase: () => {},
    toast: (message, failed) => messages.push({ message, failed }),
    api: async (url, options) => {
      requests.push({ url, options });
      if (url === "/api/cases/case-a") return context.__state.case;
      const side = url.endsWith("/anterior") ? "anterior" : url.endsWith("/posterior") ? "posterior" : null;
      assert.ok(side, `unexpected API URL: ${url}`);
      const saved = context.__state.endplanes.baselinePlanes;
      return {
        scope: "main_airway",
        mask_version: 3,
        format: "oblique_planes_v1",
        anterior_plane: side === "anterior" ? context.__state.endplanes.planes.anterior : saved.anterior,
        posterior_plane: side === "posterior" ? context.__state.endplanes.planes.posterior : saved.posterior,
        complete: true,
      };
    },
  });
  vm.runInContext([
    stateSource(),
    functionSource("validLpsVector"),
    functionSource("normalizedEndplane"),
    functionSource("cloneEndplane"),
    functionSource("cloneEndplanePair"),
    functionSource("ctIndexFromPlane"),
    functionSource("endplanePairChanged"),
    functionSource("endplaneSideChanged"),
    functionSource("updateEndplaneDirty"),
    functionSource("saveSingleEndplane"),
    functionSource("discardSingleEndplane"),
    "globalThis.__state = state;",
  ].join("\n"), context, { filename: "static/app.js" });

  const state = context.__state;
  const anteriorBaseline = plane(-1, -1);
  const posteriorBaseline = plane(2, 1);
  const anteriorDraft = plane(-2, -2);
  const posteriorDraft = plane(3, 1);
  state.caseId = "case-a";
  state.case = { case_id: "case-a" };
  state.endplanes = {
    ...state.endplanes,
    key: "case-a|main_airway|3",
    status: "ready",
    profile: {
      can_save: true,
      count: 10,
      ap_origin_lps_mm: [0, 0, 0],
      ap_direction_lps: [0, 1, 0],
      ap_spacing_mm: 1,
      saved: { format: "oblique_planes_v1", anterior_plane: anteriorBaseline, posterior_plane: posteriorBaseline },
    },
    baseline: { anterior: 1, posterior: 2 },
    baselinePlanes: { anterior: anteriorBaseline, posterior: posteriorBaseline },
    values: { anterior: 2, posterior: 3 },
    planes: { anterior: anteriorDraft, posterior: posteriorDraft },
    dirty: true,
    saving: false,
  };
  context.updateEndplaneDirty();
  return { context, state, requests, messages, anteriorBaseline, posteriorBaseline, anteriorDraft, posteriorDraft };
}

for (const side of ["anterior", "posterior"]) {
  test(`saving ${side} submits only that endplane and preserves the other draft`, async () => {
    const h = harness();
    await h.context.saveSingleEndplane(side);

    const post = h.requests[0];
    assert.equal(post.url, `/api/cases/case-a/segment/endplanes/${side}`);
    assert.equal(post.options.method, "POST");
    assert.deepEqual(Object.keys(post.options.body).sort(), ["mask_version", "plane", "scope"]);
    assert.equal(post.options.body.scope, "main_airway");
    assert.equal(post.options.body.mask_version, 3);
    assertJsonEqual(post.options.body.plane.point_lps_mm, h.state.endplanes.planes[side].point_lps_mm);
    assert.equal(h.requests[1].url, "/api/cases/case-a");
    assertJsonEqual(h.state.endplanes.planes[side], h.state.endplanes.baselinePlanes[side]);
    const other = side === "anterior" ? "posterior" : "anterior";
    assertJsonEqual(h.state.endplanes.planes[other], h[`${other}Draft`]);
    assert.equal(h.state.endplanes.dirtySides[side], false);
    assert.equal(h.state.endplanes.dirtySides[other], true);
  });
}

test("discarding one endplane keeps the opposite unsaved draft", () => {
  const h = harness();
  h.context.discardSingleEndplane("posterior");
  assertJsonEqual(h.state.endplanes.planes.posterior, h.posteriorBaseline);
  assertJsonEqual(h.state.endplanes.planes.anterior, h.anteriorDraft);
  assert.equal(h.state.endplanes.dirtySides.posterior, false);
  assert.equal(h.state.endplanes.dirtySides.anterior, true);
});

function viewerHarness() {
  const three = require("../static/vendor/three.min.js");
  const listeners = {};
  const pending = [];
  const frames = {};
  const canvas = {
    addEventListener: (name, handler) => { listeners[name] = handler; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 640, height: 420 }),
  };
  const context = vm.createContext({
    THREE: {
      ...three,
      WebGLRenderer: class {
        constructor() { this.domElement = canvas; }
        setPixelRatio() {}
        setSize() {}
        render(scene, camera) { frames.scene = scene; frames.camera = camera; scene.updateMatrixWorld(true); }
      },
      OrbitControls: class {
        constructor(camera) { this.camera = camera; this.target = new three.Vector3(); frames.orbit = this; }
        update() { this.camera.lookAt(this.target); this.camera.updateMatrixWorld(true); }
      },
      TransformControls: class extends three.Object3D {
        attach(object) { this.object = object; }
        detach() { this.object = null; }
        setMode(mode) { this.mode = mode; }
      },
    },
    window: { addEventListener() {}, setTimeout: (handler) => pending.push(handler) },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    updateEndplaneDraft() {},
    renderEndplaneUi() {},
  });
  vm.runInContext([
    stateSource(),
    ...["validLpsVector", "normalizedEndplane", "endplaneFlowDirection", "planeFromCtIndex", "ctIndexFromPlane", "isAxialEndplane",
      "normalizedProfileDirection", "planeNormalFromObject", "endplaneClippingPlanes", "createEndplaneViewer"].map(functionSource),
    "globalThis.__state = state;",
  ].join("\n"), context);
  const viewer = context.createEndplaneViewer({ clientWidth: 640, clientHeight: 420, replaceChildren() {} });
  const profile = {
    count: 40, ap_origin_lps_mm: [-100, -20, -100], ap_direction_lps: [0, 1, 0],
    ap_spacing_mm: 1, anterior_sign: -1, areas_mm2: Array(40).fill(100),
  };
  viewer.setProfile(profile);
  viewer.loadGeometry(new three.BoxGeometry(10, 10, 10, 10, 10, 10));
  frames.orbit.update();
  const markers = () => frames.scene.children.find((child) => child.children.some((object) => object.userData.kind === "endplane-marker"))?.children || [];
  return {
    three, context, viewer, profile, state: context.__state, markers,
    click() {
      const event = { clientX: 320, clientY: 210 };
      listeners.pointerdown(event);
      listeners.pointerup(event);
      pending.splice(0).forEach((handler) => handler());
    },
    setPlanes(planes) {
      context.__state.endplanes.planes = planes;
      viewer.setPlanes(planes, "anterior");
    },
  };
}

test("model click skips clipped foreground and selects the visible surface behind it", () => {
  const h = viewerHarness();
  h.setPlanes({ anterior: null, posterior: { point_lps_mm: [0, 0, 0], normal_lps: [0, 1, 0] } });
  h.click();
  assert.ok(h.state.endplanes.planes.anterior.point_lps_mm[1] < 0, "the clipped front surface must be ignored");
});

test("model click cannot place a plane when every intersection is clipped", () => {
  const h = viewerHarness();
  h.setPlanes({ anterior: null, posterior: { point_lps_mm: [0, -20, 0], normal_lps: [0, 1, 0] } });
  h.click();
  assert.equal(h.state.endplanes.planes.anterior, null);
});

test("CT-index markers stay near the airway without changing the plane or its outward direction", () => {
  const h = viewerHarness();
  const plane = { point_lps_mm: [-100, -2, -100], normal_lps: [0, -1, 0] };
  h.setPlanes({ anterior: plane, posterior: null });
  const marker = h.markers()[0];
  assert.ok(Math.abs(marker.position.x) < 1 && Math.abs(marker.position.z) < 1);
  assert.equal(marker.position.y, -2);
  assert.ok(h.context.planeNormalFromObject(marker).distanceTo(new h.three.Vector3(0, -1, 0)) < 1e-6);
  assert.ok(marker.userData.radius_mm <= 1.1, "large local areas must not produce a full-frame marker");
  assert.equal(plane.point_lps_mm[0], -100, "display recentering must not mutate saved coordinates");
});

test("flow arrows point into the airway at the nostril and toward the lung at the nasopharynx", () => {
  const context = vm.createContext({});
  vm.runInContext(functionSource("endplaneFlowDirection"), context);
  assert.deepEqual(context.endplaneFlowDirection("anterior", [0, -1, 0]), [0, 1, 0]);
  assert.deepEqual(context.endplaneFlowDirection("posterior", [0, 1, 0]), [0, 1, 0]);
});

test("marker keeps the clipping normal while exposing the flow direction separately", () => {
  const h = viewerHarness();
  h.setPlanes({
    anterior: { point_lps_mm: [0, -2, 0], normal_lps: [0, -1, 0] },
    posterior: { point_lps_mm: [0, 2, 0], normal_lps: [0, 1, 0] },
  });
  const markers = h.markers().filter((marker) => marker.userData.kind === "endplane-marker");
  assert.equal(markers.length, 2);
  const bySide = Object.fromEntries(markers.map((marker) => [marker.userData.side, marker]));
  assert.deepEqual(bySide.anterior.userData.flow_direction_lps, [0, 1, 0]);
  assert.deepEqual(bySide.posterior.userData.flow_direction_lps, [0, 1, 0]);
  assert.ok(h.context.planeNormalFromObject(bySide.anterior).distanceTo(new h.three.Vector3(0, -1, 0)) < 1e-6);
  assert.ok(h.context.planeNormalFromObject(bySide.posterior).distanceTo(new h.three.Vector3(0, 1, 0)) < 1e-6);
  for (const marker of markers) {
    const arrow = marker.children.find((child) => child.type === "ArrowHelper");
    assert.ok(arrow, "visible flow arrow is present");
    const actual = new h.three.Vector3(0, 1, 0)
      .applyQuaternion(arrow.quaternion)
      .applyQuaternion(marker.quaternion)
      .normalize();
    const expected = new h.three.Vector3(...marker.userData.flow_direction_lps).normalize();
    assert.ok(actual.distanceTo(expected) < 1e-6, `${marker.userData.side} arrow matches its flow vector`);
  }
  const anterior = bySide.anterior;
  anterior.quaternion.premultiply(new h.three.Quaternion().setFromAxisAngle(new h.three.Vector3(1, 0, 0), 0.35));
  const rotatedNormal = h.context.planeNormalFromObject(anterior);
  const rotatedFlow = new h.three.Vector3(...h.context.endplaneFlowDirection("anterior", rotatedNormal.toArray()));
  const rotatedArrow = anterior.children.find((child) => child.type === "ArrowHelper");
  const rotatedActual = new h.three.Vector3(0, 1, 0)
    .applyQuaternion(rotatedArrow.quaternion)
    .applyQuaternion(anterior.quaternion)
    .normalize();
  assert.ok(rotatedActual.distanceTo(rotatedFlow.normalize()) < 1e-6, "the arrow follows a rotated plane normal");
});

test("replacing markers releases their geometry and materials and preserves the peer marker", () => {
  const h = viewerHarness();
  const planes = {
    anterior: { point_lps_mm: [0, -2, 0], normal_lps: [0, -1, 0] },
    posterior: { point_lps_mm: [0, 2, 0], normal_lps: [0, 1, 0] },
  };
  h.setPlanes(planes);
  let resources = 0;
  let released = 0;
  h.markers().forEach((marker) => marker.traverse((child) => {
    for (const resource of [child.geometry, child.material].filter(Boolean)) {
      resources += 1;
      resource.addEventListener("dispose", () => { released += 1; });
    }
  }));
  h.setPlanes(planes);
  assert.equal(released, resources);
  assert.equal(h.markers().length, 2);
  h.viewer.clear();
  assert.equal(h.markers().length, 0);
});
