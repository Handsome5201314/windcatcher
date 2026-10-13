"use strict";

(function () {
  const COLOR_STOPS = [[0, 0.16, 0.38, 0.78], [0.25, 0.1, 0.72, 0.8], [0.5, 0.32, 0.83, 0.52], [0.75, 0.98, 0.79, 0.2], [1, 0.91, 0.23, 0.16]];
  const REGION_COLORS = { "left-upper": 0x978de0, "left-middle": 0x4fb5ce, "left-lower": 0x60b992, "right-upper": 0xd59ac4, "right-middle": 0xe2ab66, "right-lower": 0xcf866e };

  function colorAt(value, range) {
    const t = Math.max(0, Math.min(1, (value - range[0]) / (range[1] - range[0] || 1)));
    let i = 1;
    while (i < COLOR_STOPS.length - 1 && t > COLOR_STOPS[i][0]) i += 1;
    const a = COLOR_STOPS[i - 1], b = COLOR_STOPS[i];
    const f = (t - a[0]) / (b[0] - a[0]);
    return [a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f];
  }

  function isFiniteArray(values) {
    return Array.isArray(values) && values.every((value) => Number.isFinite(value));
  }

  function validateMesh(mesh, label) {
    if (!mesh || !isFiniteArray(mesh.positions) || mesh.positions.length % 3 || mesh.positions.length < 9) throw new Error(`${label}没有完整的三维坐标`);
    const n = mesh.positions.length / 3;
    if (mesh.indices && (!Array.isArray(mesh.indices) || mesh.indices.length % 3 || !mesh.indices.every((i) => Number.isInteger(i) && i >= 0 && i < n))) throw new Error(`${label}三角面索引无效`);
    for (const key of ["speeds", "pressure"]) {
      if (mesh[key] && (!isFiniteArray(mesh[key]) || mesh[key].length !== n)) throw new Error(`${label}${key}字段与网格不匹配`);
    }
  }

  function valueRange(data, field) {
    const known = data.ranges?.[field];
    if (isFiniteArray(known) && known.length === 2) return known;
    let min = Infinity, max = -Infinity;
    const sources = field === "velocity" ? [...data.streamlines, ...data.slices] : [data.wall, ...data.slices];
    for (const source of sources) for (const value of source[field === "velocity" ? "speeds" : "pressure"] || []) {
      min = Math.min(min, value); max = Math.max(max, value);
    }
    return Number.isFinite(min) ? [min, max] : null;
  }

  class CfdViewer {
    constructor(container, data, options = {}) {
      if (!window.THREE || !THREE.OrbitControls) throw new Error("三维渲染组件未加载，请刷新页面");
      if (data.units?.coordinates !== "m" || data.units?.velocity !== "m/s") throw new Error("流场坐标或速度单位未确认，无法显示结果");
      if (!["Pa", "m2/s2", "m²/s²"].includes(data.units?.pressure)) throw new Error("压力单位未确认，无法显示结果");
      if (!isFiniteArray(data.bounds_m) || data.bounds_m.length !== 6) throw new Error("流场空间范围缺失");
      data.streamlines = data.streamlines || [];
      data.slices = data.slices || [];
      validateMesh(data.wall, "气道壁面");
      for (const [name, patch] of Object.entries(data.patches || {})) validateMesh(patch, name);
      for (const slice of data.slices) validateMesh(slice, "内部切片");
      for (const line of data.streamlines) {
        if (!isFiniteArray(line.positions) || line.positions.length < 6 || line.positions.length % 3 || !isFiniteArray(line.speeds) || line.speeds.length !== line.positions.length / 3 || line.speeds.some((speed) => speed < 0)) throw new Error("流线速度与坐标不匹配");
        if (line.pressure && (!isFiniteArray(line.pressure) || line.pressure.length !== line.speeds.length)) throw new Error("流线压力与坐标不匹配");
        if (line.integration_time_s && (!isFiniteArray(line.integration_time_s) || line.integration_time_s.length !== line.speeds.length || line.integration_time_s.some((time, i, times) => i > 0 && time < times[i - 1]))) throw new Error("流线积分时间缺失或不按前进方向递增");
      }
      this.container = container;
      this.data = data;
      this.options = options;
      this.disposed = false;
      this.playing = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.visible = true;
      this.speed = 0.1;
      this.elapsed = 0;
      this.lastTime = null;
      this.field = "velocity";
      this.opacity = 0.36;
      this.sliceAxis = "none";
      this.showLines = true;
      this.showPatches = true;
      this.ranges = { velocity: valueRange(data, "velocity"), pressure: valueRange(data, "pressure") };
      const b = data.bounds_m;
      this.center = new THREE.Vector3((b[0] + b[1]) * 500, (b[2] + b[3]) * 500, (b[4] + b[5]) * 500);
      this.size = Math.max((b[1] - b[0]) * 1000, (b[3] - b[2]) * 1000, (b[5] - b[4]) * 1000);
      if (!(this.size > 0)) throw new Error("流场空间范围无效");
      this.scene = new THREE.Scene();
      this.scene.background = new THREE.Color(0x0d1519);
      this.camera = new THREE.PerspectiveCamera(40, 1, this.size / 1000, this.size * 30);
      this.camera.up.set(0, 0, 1);
      try { this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false }); }
      catch { throw new Error("此浏览器无法启用 WebGL。请启用硬件加速后刷新；计算数据仍保留。"); }
      this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      this.renderer.outputEncoding = THREE.sRGBEncoding;
      this.container.replaceChildren(this.renderer.domElement);
      this.onContextLost = (event) => {
        event.preventDefault();
        this.setVisible(false);
        this.options.onError?.("三维显示中断，请刷新结果恢复；计算数据仍保留。");
      };
      this.renderer.domElement.addEventListener("webglcontextlost", this.onContextLost);
      this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
      this.controls.enableDamping = true;
      this.analysisGroup = new THREE.Group();
      this.scene.add(this.analysisGroup);
      this.analysisVisible = true;
      this.analysisMeshes = [];
      this.analysisRaycaster = new THREE.Raycaster();
      this.onAnalysisPointerDown = (event) => {
        this.analysisPointer = event.isPrimary !== false && event.button === 0
          ? { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false } : null;
      };
      this.onAnalysisPointerMove = (event) => {
        const start = this.analysisPointer;
        if (start && start.id === event.pointerId && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 5) start.moved = true;
      };
      this.onAnalysisPointerUp = (event) => {
        const start = this.analysisPointer;
        this.analysisPointer = null;
        if (!start || start.id !== event.pointerId || start.moved || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 5 || !this.analysisGroup.visible || !this.onAnalysisSelect) return;
        const rect = this.renderer.domElement.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        this.analysisRaycaster.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), this.camera);
        const hit = this.analysisRaycaster.intersectObjects(this.analysisMeshes, false)[0];
        if (hit) this.onAnalysisSelect(hit.object.userData.regionId);
      };
      this.onAnalysisPointerCancel = () => { this.analysisPointer = null; };
      for (const [name, handler] of [["pointerdown", this.onAnalysisPointerDown], ["pointermove", this.onAnalysisPointerMove], ["pointerup", this.onAnalysisPointerUp], ["pointercancel", this.onAnalysisPointerCancel]]) this.renderer.domElement.addEventListener(name, handler);
      this.scene.add(new THREE.HemisphereLight(0xd7f2ee, 0x29343c, 0.85));
      const light = new THREE.DirectionalLight(0xffffff, 0.65);
      light.position.set(this.size, -this.size, this.size * 2);
      this.scene.add(light);
      this.meshes = [];
      this.lines = [];
      this.paths = [];
      this.wall = this.makeMesh(data.wall, new THREE.MeshLambertMaterial({ color: 0x7fa5a4, transparent: true, opacity: this.opacity, side: THREE.DoubleSide, depthWrite: false }));
      this.meshes.push({ object: this.wall, data: data.wall, type: "wall" });
      this.patches = new THREE.Group();
      this.scene.add(this.patches);
      for (const [name, patch] of Object.entries(data.patches || {})) {
        const object = this.makeMesh(patch, new THREE.MeshBasicMaterial({ color: name === "inlet" ? 0x2bb3a6 : 0xf0ad61, side: THREE.DoubleSide, transparent: true, opacity: 0.75 }), this.patches);
        object.renderOrder = 2;
      }
      for (const slice of data.slices) {
        const object = this.makeMesh(slice, new THREE.MeshBasicMaterial({ side: THREE.DoubleSide, vertexColors: true, transparent: true, opacity: 0.95 }));
        object.visible = false;
        this.meshes.push({ object, data: slice, type: "slice" });
      }
      const lineGroup = new THREE.Group();
      this.scene.add(lineGroup);
      this.lineGroup = lineGroup;
      for (const line of data.streamlines) this.addLine(line, lineGroup);
      this.setDensity(2);
      this.setField("velocity");
      this.reset();
      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(container);
      this.resize();
      this.onDocumentVisibility = () => {
        this.lastTime = null;
        if (!document.hidden && this.visible) this.start();
      };
      document.addEventListener("visibilitychange", this.onDocumentVisibility);
      this.start();
    }

    coordinates(values) {
      const output = new Float32Array(values.length);
      for (let i = 0; i < values.length; i += 3) {
        output[i] = values[i] * 1000 - this.center.x;
        output[i + 1] = values[i + 1] * 1000 - this.center.y;
        output[i + 2] = values[i + 2] * 1000 - this.center.z;
      }
      return output;
    }

    makeMesh(mesh, material, parent = this.scene) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(this.coordinates(mesh.positions), 3));
      if (mesh.indices) geometry.setIndex(mesh.indices);
      geometry.computeVertexNormals();
      const object = new THREE.Mesh(geometry, material);
      parent.add(object);
      return object;
    }

    addLine(line, parent) {
      const positions = this.coordinates(line.positions);
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const object = new THREE.Line(geometry, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.6 }));
      parent.add(object);
      this.lines.push({ object, data: line });
      // Prefer the solver-field integration time; older results derive travel from sampled speeds.
      const travel = new Float64Array(line.speeds.length);
      if (line.integration_time_s) {
        const start = line.integration_time_s[0];
        for (let i = 1; i < travel.length; i += 1) travel[i] = line.integration_time_s[i] - start;
      } else {
        for (let i = 1; i < line.speeds.length; i += 1) {
          const j = i * 3;
          const distance = Math.hypot(line.positions[j] - line.positions[j - 3], line.positions[j + 1] - line.positions[j - 2], line.positions[j + 2] - line.positions[j - 1]);
          const meanSpeed = (line.speeds[i] + line.speeds[i - 1]) / 2;
          travel[i] = travel[i - 1] + distance / Math.max(meanSpeed, 1e-12);
        }
      }
      const duration = travel[travel.length - 1];
      if (Number.isFinite(duration) && duration > 0 && line.speeds.some((v) => v > 0)) this.paths.push({ positions, speeds: line.speeds, pressure: line.pressure, travel, duration });
    }

    setColors(object, values, range) {
      const count = object.geometry.getAttribute("position").count;
      const colors = new Float32Array(count * 3);
      for (let i = 0; i < count; i += 1) colors.set(colorAt(values[i], range), i * 3);
      object.geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    }

    setField(field) {
      if (!this.ranges[field]) throw new Error(`${field === "pressure" ? "压力" : "速度"}采样尚未生成`);
      this.field = field;
      for (const mesh of this.meshes) {
        const values = field === "pressure" ? mesh.data.pressure : mesh.data.speeds;
        const colored = Boolean(values);
        mesh.object.material.vertexColors = colored;
        mesh.object.material.color.set(colored ? 0xffffff : 0x7fa5a4);
        if (colored) this.setColors(mesh.object, values, this.ranges[field]);
        mesh.object.material.needsUpdate = true;
        if (mesh.type === "slice") mesh.object.visible = colored && mesh.data.axis === this.sliceAxis;
      }
      for (const line of this.lines) {
        const values = field === "pressure" ? line.data.pressure : line.data.speeds;
        line.object.material.vertexColors = Boolean(values);
        line.object.material.color.set(values ? 0xffffff : 0xcde2de);
        if (values) this.setColors(line.object, values, this.ranges[field]);
        line.object.material.needsUpdate = true;
      }
      this.updateParticles();
      this.options.onLegend?.({ field, range: this.ranges[field], unit: field === "velocity" ? "m/s" : this.data.units.pressure });
      this.render();
    }

    setOpacity(value) { this.opacity = value; this.wall.material.opacity = value; this.wall.visible = value > 0; this.render(); }
    setSlice(axis) { this.sliceAxis = axis; this.setField(this.field); }
    setLines(visible) { this.showLines = visible; this.lineGroup.visible = visible; this.render(); }
    setPatches(visible) { this.showPatches = visible; this.patches.visible = visible; this.render(); }
    setSpeed(value) { this.speed = value; }
    setPlaying(value) { this.playing = value; this.lastTime = null; }

    clearAnalysisSection() {
      while (this.analysisGroup.children.length) {
        const object = this.analysisGroup.children[0];
        this.analysisGroup.remove(object);
        object.geometry?.dispose();
        object.material?.dispose();
      }
      this.analysisMeshes = [];
      this.onAnalysisSelect = null;
      this.render();
    }

    setAnalysisSection(result, onSelect) {
      this.clearAnalysisSection();
      if (!result) return;
      const regions = (result.region_meshes || []).filter((mesh) => mesh.positions?.length >= 9);
      for (const mesh of regions) validateMesh(mesh, mesh.label || "分析区域");
      if (result.section_mesh?.positions?.length >= 9) {
        validateMesh(result.section_mesh, "分析截面");
        const base = this.makeMesh(result.section_mesh, new THREE.MeshBasicMaterial({ color: 0xb5c7c5, side: THREE.DoubleSide, transparent: true, opacity: regions.length ? 0.2 : 0.72, depthWrite: false }), this.analysisGroup);
        base.renderOrder = 4;
      }
      for (const mesh of regions) {
        const object = this.makeMesh(mesh, new THREE.MeshBasicMaterial({ color: REGION_COLORS[mesh.id] || 0xb5c7c5, side: THREE.DoubleSide, transparent: true, opacity: 0.75, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 }), this.analysisGroup);
        object.userData.regionId = mesh.id;
        object.renderOrder = 5;
        this.analysisMeshes.push(object);
      }
      this.onAnalysisSelect = onSelect;
      this.analysisGroup.visible = this.analysisVisible;
      this.render();
    }

    setAnalysisVisible(visible) { this.analysisVisible = visible; this.analysisGroup.visible = visible; this.render(); }

    selectAnalysisRegion(id) {
      for (const object of this.analysisMeshes) {
        object.material.opacity = object.userData.regionId === id ? 1 : 0.5;
        object.renderOrder = object.userData.regionId === id ? 6 : 5;
      }
      this.render();
    }

    setDensity(count) {
      this.density = count;
      if (this.particles) {
        this.scene.remove(this.particles);
        this.particles.geometry.dispose();
        this.particles.material.dispose();
      }
      const total = this.paths.length * count;
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(total * 3), 3));
      geometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(total * 3), 3));
      this.particles = new THREE.Points(geometry, new THREE.PointsMaterial({ size: Math.max(this.size / 110, 0.5), vertexColors: true, sizeAttenuation: true, depthWrite: false }));
      this.particles.renderOrder = 3;
      this.scene.add(this.particles);
      this.updateParticles();
      this.render();
    }

    updateParticles() {
      if (!this.particles) return;
      const positions = this.particles.geometry.getAttribute("position");
      const colors = this.particles.geometry.getAttribute("color");
      let slot = 0;
      for (const path of this.paths) for (let n = 0; n < this.density; n += 1) {
        const time = (this.elapsed + path.duration * n / this.density) % path.duration;
        let lo = 0, hi = path.travel.length - 1;
        while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (path.travel[mid] <= time) lo = mid; else hi = mid; }
        const f = (time - path.travel[lo]) / (path.travel[hi] - path.travel[lo] || 1);
        const a = lo * 3, b = hi * 3;
        positions.setXYZ(slot, path.positions[a] + (path.positions[b] - path.positions[a]) * f, path.positions[a + 1] + (path.positions[b + 1] - path.positions[a + 1]) * f, path.positions[a + 2] + (path.positions[b + 2] - path.positions[a + 2]) * f);
        const values = this.field === "pressure" ? path.pressure : path.speeds;
        const c = values ? colorAt(values[lo] + (values[hi] - values[lo]) * f, this.ranges[this.field]) : [1, 1, 1];
        colors.setXYZ(slot, ...c);
        slot += 1;
      }
      positions.needsUpdate = true;
      colors.needsUpdate = true;
    }

    reset() {
      const damping = this.controls.enableDamping;
      this.controls.enableDamping = false;
      this.controls.update();
      this.camera.position.set(this.size * 1.3, -this.size * 1.5, this.size * 0.9);
      this.controls.target.set(0, 0, 0);
      this.controls.update();
      this.controls.enableDamping = damping;
      this.render();
    }

    resize() {
      if (this.disposed) return;
      const width = Math.max(this.container.clientWidth, 1), height = Math.max(this.container.clientHeight, 1);
      this.camera.aspect = width / height;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(width, height, false);
      this.render();
    }

    render() { if (!this.disposed && this.renderer) this.renderer.render(this.scene, this.camera); }
    start() {
      if (this.frame || this.disposed || !this.visible || document.hidden) return;
      const tick = (now) => {
        this.frame = null;
        if (this.disposed || !this.visible || document.hidden) return;
        const dt = this.lastTime === null ? 0 : Math.min((now - this.lastTime) / 1000, 0.1);
        this.lastTime = now;
        if (this.playing) { this.elapsed += dt * this.speed; this.updateParticles(); }
        this.controls.update();
        this.render();
        this.frame = requestAnimationFrame(tick);
      };
      this.frame = requestAnimationFrame(tick);
    }

    setVisible(visible) {
      this.visible = visible;
      this.lastTime = null;
      if (visible) { this.resize(); this.start(); }
      else if (this.frame) { cancelAnimationFrame(this.frame); this.frame = null; }
    }

    dispose() {
      if (this.disposed) return;
      this.disposed = true;
      if (this.frame) cancelAnimationFrame(this.frame);
      this.resizeObserver?.disconnect();
      document.removeEventListener("visibilitychange", this.onDocumentVisibility);
      this.controls?.dispose();
      this.scene.traverse((object) => {
        object.geometry?.dispose();
        if (Array.isArray(object.material)) object.material.forEach((material) => material.dispose());
        else object.material?.dispose();
      });
      this.renderer.domElement.removeEventListener("webglcontextlost", this.onContextLost);
      for (const [name, handler] of [["pointerdown", this.onAnalysisPointerDown], ["pointermove", this.onAnalysisPointerMove], ["pointerup", this.onAnalysisPointerUp], ["pointercancel", this.onAnalysisPointerCancel]]) this.renderer.domElement.removeEventListener(name, handler);
      this.renderer.dispose();
      this.renderer.forceContextLoss();
      this.container.replaceChildren();
    }
  }

  window.CfdViewer = CfdViewer;
})();
