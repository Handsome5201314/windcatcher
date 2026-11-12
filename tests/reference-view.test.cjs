"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const THREE = require('../static/vendor/three.min.js');
const window = {};
vm.runInNewContext(fs.readFileSync('static/cfd-viewer.js', 'utf8'), {window, THREE});
const proto = window.CfdViewer.prototype;

test('anatomy hides all flow layers and survives field and density changes', () => {
  const subject = { patientPresentation: true, wall: new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshLambertMaterial()),
    lineGroup: {visible: true}, particles: {visible: true}, patches: {visible: true},
    meshes: [], lines: [], ranges: {velocity: [0, 3]}, data: {units: {pressure: 'Pa'}}, options: {},
    render() {}, updateParticles() {}, setOpacity: proto.setOpacity };
  proto.setPresentationMode.call(subject, 'anatomy');
  assert.equal(subject.particles.visible, false);
  assert.equal(subject.lineGroup.visible, false);
  assert.equal(subject.wall.material.vertexColors, false);
  assert.equal(subject.wall.material.opacity, 1);
  proto.setField.call(subject, 'velocity');
  assert.equal(subject.lineGroup.visible, false);
  proto.setPresentationMode.call(subject, 'flow');
  assert.equal(subject.particles.visible, true);
});

test('synchronized view uses each model center and viewport fit distance', () => {
  function fixture(size, aspect) {
    const subject = {patientPresentation:true, camera:new THREE.PerspectiveCamera(40, aspect, .001, 1e5),
      frameRadius: size, frameHalf: new THREE.Vector3(size, size, size),
      frameCenter: new THREE.Vector3(size*10, 0, 0),
      controls:{target:new THREE.Vector3(), enableDamping:true, update(){}}, render(){},
      frameDistanceForPose:proto.frameDistanceForPose};
    subject.controls.target.copy(subject.frameCenter);
    return subject;
  }
  const target=fixture(80,.5);
  proto.applyPresentationState.call(target, {azimuth:1.2,elevation:.3,zoom:1.4});
  const state=proto.getPresentationState.call(target);
  assert.ok(Math.abs(state.azimuth-1.2)<1e-8);
  assert.ok(Math.abs(state.zoom-1.4)<1e-8);
  assert.equal(target.controls.target.x,800);
});

function referenceHelpers() {
  const context = {window:{}, document:{getElementById(){return null;}}};
  vm.runInNewContext(fs.readFileSync('static/reference-view.js','utf8'), context);
  return context.window.WindcatcherReferences;
}
const conditions={solver:'simpleFoam',turbulence_model:'laminar',inlet_mode:'flow',total_flow_lpm:15,nu:1.5e-5,rho_kg_m3:1.2};
function model(range=[0,3], unit='Pa') { return {ranges:{velocity:range,pressure:[-1,10]},units:{coordinates:'m',velocity:'m/s',pressure:unit}}; }
test('comparison combines actual data ranges and refuses unverified or mismatched CFD',()=>{
  const helpers=referenceHelpers();
  const meta={kind:'flow',flow_verified:true,conditions};
  const result=helpers.comparisonPolicy(model(),meta,model([.2,8]),meta);
  assert.equal(result.flowAllowed,true);
  assert.deepEqual(Array.from(result.ranges.velocity),[0,8]);
  assert.equal(helpers.comparisonPolicy(model(),meta,model(),{...meta,flow_verified:false}).flowAllowed,false);
  assert.equal(helpers.comparisonPolicy(model(),meta,model(),{...meta,conditions:{...conditions,total_flow_lpm:30}}).flowAllowed,false);
  assert.equal(helpers.comparisonPolicy(model(),meta,model([0,2],'m2/s2'),meta).flowAllowed,false);
  assert.equal(helpers.comparisonPolicy(model(),meta,model(),{...meta,conditions:{}}).flowAllowed,false);
  const testMeta={...meta,kind:'flow_test',flow_verified:false};
  const preview=helpers.comparisonPolicy(model(),testMeta,model([.2,8]),meta);
  assert.equal(preview.flowAllowed,true);
  assert.match(preview.reason,/未复核测试/);
  assert.equal(helpers.comparisonPolicy(model(),{...meta,flow_verified:false},model(),meta).flowAllowed,false);
});
