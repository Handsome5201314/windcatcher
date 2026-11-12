"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const THREE = require("../static/vendor/three.min.js");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "cfd-viewer.js"), "utf8");
const window = {};
vm.runInNewContext(source, { window, THREE });
const viewer = window.CfdViewer.prototype;

function fixture(width, height, patientPresentation = true) {
  const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 1000);
  camera.up.set(0, 0, 1);
  const wall = new THREE.Mesh(new THREE.BoxGeometry(20, 20, 20));
  wall.position.set(180, 25, -15);
  const controls = {
    target: new THREE.Vector3(), enableDamping: true,
    update() { camera.lookAt(this.target); camera.updateMatrixWorld(); },
  };
  const subject = {
    camera, wall, controls, size: 20, patientPresentation,
    patches: new THREE.Group(), lineGroup: new THREE.Group(), lines: [],
    container: { clientWidth: width, clientHeight: height },
    renderer: { setSize() {} }, frameDistanceForPose: viewer.frameDistanceForPose,
    render() {}, disposed: false,
  };
  viewer.reset.call(subject);
  return subject;
}

test("patient camera fits actual geometry even when server bounds center differs", () => {
  const subject = fixture(320, 560);
  assert.ok(subject.controls.target.distanceTo(new THREE.Vector3(180, 25, -15)) < 1e-6);
  assert.equal(subject.camera.aspect, 320 / 560);
  assert.ok(subject.controls.minDistance > 0);
  subject.camera.updateProjectionMatrix();
  for (const x of [170, 190]) for (const y of [15, 35]) for (const z of [-25, -5]) {
    const projected = new THREE.Vector3(x, y, z).project(subject.camera);
    assert.ok(Math.abs(projected.x) < 1 && Math.abs(projected.y) < 1,
      "geometry corner must stay inside the portrait viewport");
  }
});

test("portrait view backs camera farther away and resize preserves framing", () => {
  const landscape = fixture(560, 320);
  const portrait = fixture(320, 560);
  assert.ok(portrait.frameDistance > landscape.frameDistance);
  const target = landscape.controls.target.clone();
  landscape.container.clientWidth = 320;
  landscape.container.clientHeight = 560;
  viewer.resize.call(landscape);
  assert.ok(landscape.camera.position.distanceTo(target) >= landscape.frameDistance - 1e-6);
  assert.ok(landscape.controls.target.distanceTo(target) < 1e-6);
});

test("guided presentation changes view without moving the frame center", () => {
  const subject = fixture(320, 560);
  subject.setOpacity = value => { subject.wallOpacity = value; };
  subject.particles = { visible: true };
  viewer.setPresentationMode.call(subject, "anatomy");
  assert.equal(subject.lineGroup.visible, false);
  assert.equal(subject.particles.visible, false);
  viewer.setPresentationMode.call(subject, "flow");
  assert.equal(subject.lineGroup.visible, true);
  assert.equal(subject.particles.visible, true);
  viewer.setPresentationPose.call(subject, 1.2, 0.3);
  assert.ok(subject.controls.target.distanceTo(subject.frameCenter) < 1e-6);
  assert.ok(Math.abs(subject.camera.position.distanceTo(subject.frameCenter) - subject.frameDistance) < 1e-6);
});

test("guided orbit keeps the complete model in portrait and landscape frames", () => {
  for (const [width, height] of [[320, 560], [560, 320]]) {
    const subject = fixture(width, height);
    for (const azimuth of [-1.1, 0, 1.5, 3.1]) {
      viewer.setPresentationPose.call(subject, azimuth, 0.32);
      for (const x of [170, 190]) for (const y of [15, 35]) for (const z of [-25, -5]) {
        const projected = new THREE.Vector3(x, y, z).project(subject.camera);
        assert.ok(Math.abs(projected.x) < 0.94 && Math.abs(projected.y) < 0.94,
          "guided camera must leave a visible margin around the full model");
      }
    }
  }
});

test("research viewer keeps its existing camera reset behavior", () => {
  const subject = fixture(320, 560, false);
  assert.ok(subject.controls.target.length() < 1e-6);
  assert.ok(subject.camera.position.distanceTo(new THREE.Vector3(26, -30, 18)) < 1e-6);
});

test("operator pose frames the actual model without changing the solver data", () => {
  const subject = fixture(320, 560, false);
  const original = subject.camera.position.clone();
  viewer.applyOperatorPose.call(subject, { azimuth: 10, elevation: 0, zoom: 1 });
  assert.ok(subject.camera.position.distanceTo(original) < 1e-6);
  viewer.applyOperatorPose.call(subject, { azimuth: 0.4, elevation: 0.2, zoom: 1.3 });
  assert.ok(subject.controls.target.distanceTo(new THREE.Vector3(180, 25, -15)) < 1e-6);
  for (const x of [170, 190]) for (const y of [15, 35]) for (const z of [-25, -5]) {
    const projected = new THREE.Vector3(x, y, z).project(subject.camera);
    assert.ok(Math.abs(projected.x) < 1 && Math.abs(projected.y) < 1);
  }
});

test("legend gradient uses the renderer color scale and its display color space", () => {
  let legend;
  const subject = {
    ranges: { pressure: [-1.58, 23.14] }, meshes: [], lines: [],
    data: { units: { pressure: "Pa" } },
    updateParticles() {}, render() {}, options: { onLegend: value => { legend = value; } },
  };
  viewer.setField.call(subject, "pressure");
  const low = new THREE.Color(0.16, 0.38, 0.78).convertLinearToSRGB().getStyle();
  const high = new THREE.Color(0.91, 0.23, 0.16).convertLinearToSRGB().getStyle();
  assert.ok(legend.gradient?.startsWith("linear-gradient(to right, " + low + " 0%"));
  assert.ok(legend.gradient?.endsWith(high + " 100%)"));
  assert.equal(legend.field, "pressure");
  assert.equal(legend.unit, "Pa");
});
