"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");

// Evaluate production functions without app startup, network requests, or case writes.
function functionSource(name) {
  const start = appSource.match(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.ok(start, `Production function ${name} exists`);
  const end = appSource.indexOf("\n}", start.index);
  assert.notEqual(end, -1, `Production function ${name} is complete`);
  return appSource.slice(start.index, end + 2);
}

function stateSource() {
  const start = appSource.indexOf("const state = {");
  const end = appSource.indexOf("\n};", start);
  assert.ok(start >= 0 && end > start, "Production state exists");
  return appSource.slice(start, end + 3);
}

function element() {
  const classes = new Set();
  return {
    hidden: false,
    textContent: "",
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
      contains: (name) => classes.has(name),
    },
    setAttribute() {},
  };
}

function harness(names, overrides = {}) {
  const nodes = new Map();
  const calls = [];
  const context = vm.createContext({
    window: {},
    $: (id) => {
      if (!nodes.has(id)) nodes.set(id, element());
      return nodes.get(id);
    },
    guardEndplaneDraft: () => true,
    refreshViewer: () => calls.push("refreshViewer"),
    renderCase: () => calls.push("renderCase"),
    renderSteps: () => {},
    busy: (_button, active) => calls.push(active ? "busy" : "idle"),
    startCasePolling: () => {},
    stopCasePolling: () => {},
    hasRunningStages: () => false,
    focusStageTarget: () => calls.push("focusViewer"),
    renderErrorPanel: (detail) => calls.push({ error: detail }),
    toast: (message, failed) => calls.push({ toast: message, failed }),
    ...overrides,
  });
  vm.runInContext(
    [stateSource(), ...names.map(functionSource), "globalThis.__state = state;"].join("\n"),
    context,
    { filename: "static/app.js" },
  );
  return { context, state: context.__state, calls, nodes };
}

function caseData(status = "ok", caseId = "case-a", updatedAt = "2026-10-07T10:00:00Z", version = 4) {
  return {
    case_id: caseId,
    stages: {
      import: { status: "ok" },
      segment: { status: "ok", data: { mask_version: version } },
      surface: { status, updated_at: updatedAt },
    },
  };
}

function setCase(h, data = caseData()) {
  h.state.case = data;
  h.state.caseId = data.case_id;
}

test("STL activation closes editing and forces a fresh model after generation", () => {
  const h = harness(["activateStlPreview"]);
  setCase(h);
  const savedPlanes = { anterior: { point_lps_mm: [1, 2, 3] }, posterior: { point_lps_mm: [4, 5, 6] } };
  h.state.endplanes.open = true;
  h.state.endplanes.planes = savedPlanes;
  h.state.maskEditor.active = true;
  h.state.mode = "slices";
  h.state.viewer3dCaseId = "case-a";
  h.state.viewer3dKey = "old-model";
  let cleared = 0;
  h.state.viewer3d = { clear: () => cleared++ };

  assert.equal(h.context.activateStlPreview({ reload: true }), true);
  assert.equal(h.state.endplanes.open, false);
  assert.equal(h.state.endplanes.planes, savedPlanes);
  assert.equal(h.state.maskEditor.active, false);
  assert.equal(h.state.mode, "3d");
  assert.equal(h.state.viewer3dCaseId, null);
  assert.equal(h.state.viewer3dKey, null);
  assert.equal(cleared, 1);
  assert.deepEqual(h.calls, ["refreshViewer"]);
});

test("a rejected unsaved-endplane guard prevents any STL activation", () => {
  const h = harness(["activateStlPreview"], { guardEndplaneDraft: () => false });
  h.state.endplanes.open = true;
  h.state.maskEditor.active = true;
  h.state.mode = "slices";
  h.state.viewer3dCaseId = "case-a";
  h.state.viewer3dKey = "unchanged";
  h.state.viewer3d = { clear: () => assert.fail("Guard must precede clearing") };

  assert.equal(h.context.activateStlPreview({ reload: true }), false);
  assert.equal(h.state.endplanes.open, true);
  assert.equal(h.state.maskEditor.active, true);
  assert.equal(h.state.mode, "slices");
  assert.equal(h.state.viewer3dCaseId, "case-a");
  assert.equal(h.state.viewer3dKey, "unchanged");
  assert.deepEqual(h.calls, []);
});

test("manual STL activation keeps an already current cache and pauses the hidden editor", () => {
  const h = harness(["activateStlPreview", "renderEndplaneUi"]);
  h.state.endplanes.open = true;
  h.state.viewer3dCaseId = "case-a";
  h.state.viewer3dKey = "current";
  h.state.viewer3d = { clear: () => assert.fail("Manual activation must not force regeneration") };
  let paused = 0;
  h.state.endplanes.viewer = { pause: () => paused++ };

  assert.equal(h.context.activateStlPreview(), true);
  h.context.renderEndplaneUi();
  assert.equal(h.nodes.get("endplane-panel").hidden, true);
  assert.equal(paused, 1);
  assert.equal(h.state.viewer3dCaseId, "case-a");
  assert.equal(h.state.viewer3dKey, "current");
});

test("surface availability accepts warning models and rejects missing or failed models", () => {
  const h = harness(["hasSurfaceModel"]);
  for (const status of ["ok", "warn", "pending", "running", "failed", "blocked"]) {
    setCase(h, caseData(status));
    assert.equal(h.context.hasSurfaceModel(), ["ok", "warn"].includes(status), status);
  }
  h.state.caseId = null;
  assert.equal(h.context.hasSurfaceModel(), false);
});

test("surface model identity changes with case, generation time, or mask version", () => {
  const h = harness(["surfaceModelKey"]);
  setCase(h);
  const original = h.context.surfaceModelKey();
  assert.equal(h.context.surfaceModelKey(), original);
  h.state.case.stages.surface.updated_at = "2026-10-07T11:00:00Z";
  const regenerated = h.context.surfaceModelKey();
  assert.notEqual(regenerated, original);
  h.state.case.stages.segment.data.mask_version += 1;
  assert.notEqual(h.context.surfaceModelKey(), regenerated);
  h.state.caseId = "case-b";
  assert.notEqual(h.context.surfaceModelKey(), original);
  h.state.case.stages.segment.data.mask_version = 0;
  const zeroVersion = h.context.surfaceModelKey();
  delete h.state.case.stages.segment.data.mask_version;
  assert.notEqual(h.context.surfaceModelKey(), zeroVersion);
});

for (const verdict of ["pass", "warn"]) {
  test(`successful STL generation with geometry verdict ${verdict} opens the latest formal model`, async () => {
    const fresh = caseData(verdict === "warn" ? "warn" : "ok");
    const h = harness(["runAction", "hasSurfaceModel", "activateStlPreview"], {
      api: async (url) => {
        assert.equal(url, "/api/cases/case-a");
        return fresh;
      },
    });
    setCase(h);
    h.state.endplanes.open = true;
    h.state.maskEditor.active = true;
    h.state.mode = "slices";
    h.state.viewer3dKey = "old-model";
    h.state.viewer3dCaseId = "case-a";
    let clears = 0;
    h.state.viewer3d = { clear: () => clears++ };

    await h.context.runAction({}, async () => ({ status: "ok", geom_qc: { verdict } }), "surface");
    assert.equal(h.state.case, fresh);
    assert.equal(h.state.endplanes.open, false);
    assert.equal(h.state.maskEditor.active, false);
    assert.equal(h.state.mode, "3d");
    assert.equal(h.state.viewer3dKey, null);
    assert.equal(h.state.viewer3dCaseId, null);
    assert.equal(clears, 1);
    assert.ok(h.calls.indexOf("renderCase") < h.calls.indexOf("refreshViewer"));
    assert.ok(h.calls.includes("focusViewer"));
  });
}

for (const result of [
  { status: "failed", message: "Surface generation failed" },
  { status: "blocked", message: "Mask review required" },
  { status: "ok", geom_qc: { verdict: "fail", reasons: ["Open wall"] } },
]) {
  test(`failed STL generation never switches to a successful preview: ${result.message || "geometry fail"}`, async () => {
    const h = harness(["runAction", "hasSurfaceModel", "activateStlPreview"], {
      api: async () => caseData("failed"),
    });
    setCase(h);
    h.state.endplanes.open = true;
    h.state.mode = "slices";
    h.state.viewer3d = { clear: () => assert.fail("Failure must not activate STL") };

    await h.context.runAction({}, async () => result, "surface");
    assert.equal(h.state.endplanes.open, true);
    assert.equal(h.state.mode, "slices");
    assert.equal(h.calls.includes("refreshViewer"), false);
    assert.equal(h.calls.includes("focusViewer"), false);
    assert.ok(h.calls.some((call) => call.toast && call.failed));
  });
}

test("a completed action for a previous case cannot replace or activate the new case", async () => {
  let resolveFresh;
  const freshPromise = new Promise((resolve) => { resolveFresh = resolve; });
  const h = harness(["runAction", "hasSurfaceModel", "activateStlPreview"], { api: () => freshPromise });
  setCase(h);
  const pending = h.context.runAction({}, async () => ({ status: "ok" }), "surface");
  await Promise.resolve();
  const newCase = caseData("ok", "case-b");
  setCase(h, newCase);
  h.state.mode = "slices";
  h.state.endplanes.open = true;
  resolveFresh(caseData());
  await pending;

  assert.equal(h.state.case, newCase);
  assert.equal(h.state.caseId, "case-b");
  assert.equal(h.state.mode, "slices");
  assert.equal(h.state.endplanes.open, true);
  assert.equal(h.calls.includes("renderCase"), false);
  assert.equal(h.calls.includes("refreshViewer"), false);
});

test("formal preview reloads a regenerated same-case STL and ignores prior-case failure callbacks", () => {
  const loads = [];
  const h = harness(["show3d", "surfaceModelKey"], {
    show3dBlocked: (message) => assert.fail(`Stale callback blocked current model: ${message}`),
  });
  setCase(h);
  h.state.viewer3d = {
    resize() {},
    load: (url, onError, onLoad) => loads.push({ url, onError, onLoad }),
  };

  h.context.show3d();
  assert.equal(loads.length, 1);
  assert.match(loads[0].url, /^\/api\/cases\/case-a\/model\?v=/);
  assert.ok(decodeURIComponent(loads[0].url).includes("2026-10-07T10:00:00Z"));
  loads[0].onLoad();
  h.context.show3d();
  assert.equal(loads.length, 1);
  h.state.case.stages.surface.updated_at = "2026-10-07T11:00:00Z";
  h.context.show3d();
  assert.equal(loads.length, 2);
  assert.notEqual(loads[1].url, loads[0].url);
  setCase(h, caseData("ok", "case-b"));
  h.context.show3d();
  loads[0].onError();
  assert.equal(h.state.viewer3dCaseId, "case-b");
  assert.equal(h.state.viewer3dKey, h.context.surfaceModelKey());
  loads[0].onLoad();
  assert.equal(h.nodes.get("viewer-sub").textContent, "正在加载最新裁切 STL…");
});

function threeHarness() {
  const requests = [];
  const groups = [];
  class Vector3 {
    constructor(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z; }
    copy(v) { this.x = v.x; this.y = v.y; this.z = v.z; return this; }
    clone() { return new Vector3(this.x, this.y, this.z); }
    add(v) { this.x += v.x; this.y += v.y; this.z += v.z; return this; }
    set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; }
    negate() { this.x = -this.x; this.y = -this.y; this.z = -this.z; return this; }
    dot(v) { return this.x * v.x + this.y * v.y + this.z * v.z; }
  }
  class Group {
    constructor() { this.children = []; groups.push(this); }
    add(value) { this.children.push(value); }
    remove(value) { this.children = this.children.filter((child) => child !== value); }
  }
  class Material {
    constructor(options) { Object.assign(this, options); this.disposed = 0; }
    dispose() { this.disposed += 1; }
  }
  class Plane {
    setFromNormalAndCoplanarPoint(normal, point) {
      this.normal = normal;
      this.constant = -normal.dot(point);
      return this;
    }
    distanceToPoint(point) { return this.normal.dot(point) + this.constant; }
  }
  const THREE = {
    Vector3, Group, Plane,
    Scene: Group,
    Color: class {},
    PerspectiveCamera: class {
      constructor() { this.position = new Vector3(); }
      updateProjectionMatrix() {}
    },
    WebGLRenderer: class {
      constructor() { this.domElement = {}; }
      setPixelRatio() {}
      setSize() {}
      render() {}
    },
    HemisphereLight: class {},
    DirectionalLight: class { constructor() { this.position = new Vector3(); } },
    OrbitControls: class {
      constructor() { this.target = new Vector3(); }
      update() {}
    },
    Box3: class {
      setFromObject(group) { this.group = group; return this; }
      isEmpty() { return !this.group.children.length; }
      getSize(vector) { return vector.set(10, 20, 30); }
      getCenter(vector) { return vector.set(0, 0, 0); }
    },
    MeshPhysicalMaterial: Material,
    Mesh: class { constructor(geometry, material) { this.geometry = geometry; this.material = material; } },
    STLLoader: class {
      load(url, onLoad, _onProgress, onError) { requests.push({ url, onLoad, onError }); }
    },
  };
  const h = harness(["createViewer3d"], {
    THREE,
    window: { devicePixelRatio: 1, addEventListener() {} },
    requestAnimationFrame: () => 1,
  });
  const viewer = h.context.createViewer3d({ clientWidth: 640, clientHeight: 520, appendChild() {} });
  return { ...h, viewer, requests, group: groups[1], THREE };
}

function geometry(name) {
  return { name, disposed: 0, normals: 0, dispose() { this.disposed += 1; }, computeVertexNormals() { this.normals += 1; } };
}

test("late STL success cannot overwrite the newer model and its geometry is disposed", () => {
  const h = threeHarness();
  let oldLoaded = 0;
  let newLoaded = 0;
  h.viewer.load("old.stl", () => assert.fail("Old error surfaced"), () => oldLoaded++);
  h.viewer.load("new.stl", () => assert.fail("Unexpected current error"), () => newLoaded++);
  const current = geometry("new");
  h.requests[1].onLoad(current);
  const stale = geometry("old");
  h.requests[0].onLoad(stale);

  assert.equal(h.group.children.length, 1);
  assert.equal(h.group.children[0].geometry, current);
  assert.equal(current.normals, 1);
  assert.equal(stale.disposed, 1);
  assert.equal(stale.normals, 0);
  assert.equal(oldLoaded, 0);
  assert.equal(newLoaded, 1);
});

test("STL reload disposes the old mesh and failed loading leaves no stale model", () => {
  const h = threeHarness();
  h.viewer.load("first.stl");
  const first = geometry("first");
  h.requests[0].onLoad(first);
  const oldMaterial = h.group.children[0].material;
  let failed = 0;
  h.viewer.load("second.stl", () => failed++);

  assert.equal(h.group.children.length, 0);
  assert.equal(first.disposed, 1);
  assert.equal(oldMaterial.disposed, 1);
  h.requests[1].onError(new Error("Missing STL"));
  assert.equal(failed, 1);
  assert.equal(h.group.children.length, 0);
});

test("clear invalidates outstanding STL requests and ignores their errors", () => {
  const h = threeHarness();
  h.viewer.load("previous-case.stl", () => assert.fail("Cancelled load error surfaced"), () => assert.fail("Cancelled load completed"));
  h.viewer.clear();
  h.requests[0].onError(new Error("Previous case failed"));
  const late = geometry("late");
  h.requests[0].onLoad(late);
  assert.equal(late.disposed, 1);
  assert.equal(h.group.children.length, 0);
});

test("3D clipping retains the same half-spaces as backend outward-normal cutting", () => {
  const { THREE } = threeHarness();
  const h = harness(["validLpsVector", "normalizedEndplane", "endplaneClippingPlanes"], { THREE });
  const planes = h.context.endplaneClippingPlanes({
    anterior: { point_lps_mm: [0, 5, 0], normal_lps: [0, 2, 0] },
    posterior: { point_lps_mm: [0, -5, 0], normal_lps: [0, -3, 0] },
  });
  assert.equal(planes.length, 2);
  const inside = new THREE.Vector3(0, 0, 0);
  assert.ok(planes.every((plane) => plane.distanceToPoint(inside) > 0));
  assert.ok(planes[0].distanceToPoint(new THREE.Vector3(0, 6, 0)) < 0);
  assert.ok(planes[1].distanceToPoint(new THREE.Vector3(0, -6, 0)) < 0);
  assert.equal(planes[0].distanceToPoint(new THREE.Vector3(0, 5, 0)), 0);

  const obliquePoint = [3, 4, 5];
  const oblique = h.context.endplaneClippingPlanes({
    anterior: { point_lps_mm: obliquePoint, normal_lps: [2, 3, -4] },
  })[0];
  for (const point of [[0, 0, 0], [9, 0, 1], [-3, 9, 4], [1, -1, 8]]) {
    const outwardDistance = point.reduce((sum, value, axis) => sum + (value - obliquePoint[axis]) * [2, 3, -4][axis], 0);
    assert.equal(oblique.distanceToPoint(new THREE.Vector3(...point)) >= 0, outwardDistance <= 0);
  }
  assert.equal(h.context.endplaneClippingPlanes({ anterior: { point_lps_mm: [0, 0, 0], normal_lps: [0, 0, 0] } }).length, 0);
});
