/* Draft-only opening annotation helpers. No persistence or CFD boundary writes. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.EndplaneOpeningMarkup = api;
})(typeof window === "undefined" ? globalThis : window, function () {
  "use strict";

  const SIDES = new Set(["left", "right", "posterior", "anterior"]);

  function finitePoint(value) {
    return Array.isArray(value) && value.length === 3 && value.every(Number.isFinite)
      ? value.map(Number) : null;
  }

  function normalize(side, value) {
    const safeSide = SIDES.has(side) ? side : "left";
    const seed = finitePoint(value?.seed ?? value?.seed_lps_mm);
    const boundary = Array.isArray(value?.boundary_loop_lps_mm)
      ? value.boundary_loop_lps_mm.map(finitePoint).filter(Boolean) : [];
    const rawOutline = value?.outline ?? value?.outline_lps_mm ?? boundary;
    const outline = Array.isArray(rawOutline)
      ? rawOutline.map(finitePoint).filter(Boolean).slice(0, 256) : [];
    const faceIds = Array.isArray(value?.selected_face_ids) &&
      value.selected_face_ids.every((id) => Number.isInteger(id) && id >= 0)
      ? value.selected_face_ids.slice() : [];
    const errors = Array.isArray(value?.quality_errors) ? value.quality_errors.slice() : [];
    const patch = faceIds.length > 0;
    const status = patch ? value?.ok === true && errors.length === 0
      ? "patch_draft" : "patch_invalid"
      : errors.length ? "patch_invalid"
      : outline.length >= 3 ? "outline_draft" : seed ? "seeded" : "empty";
    return {
      opening_id: `${safeSide}_nostril_opening`,
      side: safeSide,
      seed_lps_mm: seed,
      outline_lps_mm: outline,
      seed_face_id: Number.isInteger(value?.seed_face_id) && value.seed_face_id >= 0
        ? value.seed_face_id : null,
      selected_face_ids: faceIds,
      boundary_loop_lps_mm: boundary,
      centroid_lps_mm: finitePoint(value?.centroid_lps_mm),
      area_mm2: Number.isFinite(value?.area_mm2) && value.area_mm2 > 0
        ? value.area_mm2 : null,
      max_plane_deviation_mm: Number.isFinite(value?.max_plane_deviation_mm) &&
        value.max_plane_deviation_mm >= 0 ? value.max_plane_deviation_mm : null,
      normal_lps: finitePoint(value?.normal_lps),
      plane: value?.plane && finitePoint(value.plane.point_lps_mm) &&
        finitePoint(value.plane.normal_lps) ? {
          point_lps_mm: finitePoint(value.plane.point_lps_mm),
          normal_lps: finitePoint(value.plane.normal_lps),
        } : null,
      quality_errors: errors,
      warnings: Array.isArray(value?.warnings) ? value.warnings.slice() : [],
      ok: patch && value?.ok === true && errors.length === 0,
      status,
      source: patch ? "three_d_mesh_patch_draft" : "three_d_model_draft",
      persisted: false,
    };
  }

  function empty(side) { return normalize(side, {}); }

  function withSeed(current, seed) {
    return normalize(current?.side, { seed });
  }

  function withOutline(current, outline) {
    return normalize(current?.side, { seed: current?.seed_lps_mm, outline });
  }

  function summary(markup) {
    const value = normalize(markup?.side, markup);
    if (value.status === "patch_draft" || value.status === "patch_invalid") return {
      id: value.opening_id,
      boundary: value.boundary_loop_lps_mm.length > 0
        ? String(value.boundary_loop_lps_mm.length - 1) + " 个真实网格边界点" : "未形成单一边界环",
      status: value.status === "patch_draft"
        ? "已圈选局部网格面 · 待校验" : "局部圈选未通过几何检查",
      canConvert: false,
    };
    return {
      id: value.opening_id,
      boundary: value.outline_lps_mm.length >= 3
        ? `${value.outline_lps_mm.length} 个三维点 · 待生成闭合区域`
        : "未生成",
      status: value.status === "outline_draft" ? "已圈选边界 · 草稿"
        : value.status === "seeded" ? "已定位入口内部 · 草稿" : "尚未标注三维开口",
      canConvert: false,
    };
  }

  // positions: original non-indexed STL triangles in LPS mm, 9 numbers per face.
  // projectedPoints: one screen-space [x, y] centroid per original face.
  function point2(value) {
    if (Array.isArray(value) && value.length === 2 && value.every(Number.isFinite))
      return value.slice();
    if (value && Number.isFinite(value.x) && Number.isFinite(value.y))
      return [value.x, value.y];
    return null;
  }

  function outline2(value) {
    if (!Array.isArray(value)) return null;
    const points = value.map(point2);
    if (points.some((point) => !point)) return null;
    if (points.length > 1 && points[0][0] === points.at(-1)[0] &&
        points[0][1] === points.at(-1)[1]) points.pop();
    if (points.length < 3) return null;
    const twiceArea = points.reduce((sum, point, index) => {
      const next = points[(index + 1) % points.length];
      return sum + point[0] * next[1] - next[0] * point[1];
    }, 0);
    if (Math.abs(twiceArea) <= 1e-6) return null;
    const orientation = (a, b, c) =>
      (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    for (let i = 0; i < points.length; i += 1) {
      for (let j = i + 2; j < points.length; j += 1) {
        if (i === 0 && j === points.length - 1) continue;
        const a = points[i], b = points[(i + 1) % points.length];
        const c = points[j], d = points[(j + 1) % points.length];
        const abC = orientation(a, b, c), abD = orientation(a, b, d);
        const cdA = orientation(c, d, a), cdB = orientation(c, d, b);
        const boxesOverlap = Math.max(Math.min(a[0], b[0]), Math.min(c[0], d[0])) <=
            Math.min(Math.max(a[0], b[0]), Math.max(c[0], d[0])) &&
          Math.max(Math.min(a[1], b[1]), Math.min(c[1], d[1])) <=
            Math.min(Math.max(a[1], b[1]), Math.max(c[1], d[1]));
        if (boxesOverlap && abC * abD <= 0 && cdA * cdB <= 0) return null;
      }
    }
    return points;
  }

  function inPolygon(point, polygon) {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
      const a = polygon[j], b = polygon[i];
      const cross2 = (point[0] - a[0]) * (b[1] - a[1]) -
        (point[1] - a[1]) * (b[0] - a[0]);
      if (Math.abs(cross2) <= 1e-8 &&
          point[0] >= Math.min(a[0], b[0]) - 1e-8 &&
          point[0] <= Math.max(a[0], b[0]) + 1e-8 &&
          point[1] >= Math.min(a[1], b[1]) - 1e-8 &&
          point[1] <= Math.max(a[1], b[1]) + 1e-8) return true;
      if ((a[1] > point[1]) !== (b[1] > point[1]) &&
          point[0] < (b[0] - a[0]) * (point[1] - a[1]) / (b[1] - a[1]) + a[0])
        inside = !inside;
    }
    return inside;
  }

  function subtract(a, b) { return a.map((value, index) => value - b[index]); }
  function cross3(a, b) {
    return [a[1] * b[2] - a[2] * b[1],
      a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  }
  function dot(a, b) { return a.reduce((sum, value, i) => sum + value * b[i], 0); }
  function edgeId(a, b) { return a < b ? a + "|" + b : b + "|" + a; }

  function buildPortPatch({ positions, projectedPoints, polygon, seedFaceId = null,
    eligibleFaceIds = null, weldToleranceMm = 1e-3, planeToleranceMm = 0.5 } = {}) {
    const quality_errors = [], warnings = [];
    const result = { ok: false, selected_face_ids: [], boundary_loop_lps_mm: [],
      centroid_lps_mm: null, seed_lps_mm: null, seed_face_id: null, area_mm2: 0, normal_lps: null,
      plane: null, max_plane_deviation_mm: null, quality_errors, warnings };
    const fail = (code, message) => quality_errors.push({ code, message });
    const count = positions && typeof positions.length === "number" ? positions.length / 9 : NaN;
    if ((!ArrayBuffer.isView(positions) && !Array.isArray(positions)) ||
        !Number.isInteger(count) || count < 1) {
      fail("invalid_positions", "需要非索引 STL 三角面坐标");
      return result;
    }
    if (!Number.isFinite(weldToleranceMm) || weldToleranceMm <= 0 ||
        !Number.isFinite(planeToleranceMm) || planeToleranceMm <= 0) {
      fail("invalid_tolerance", "几何容差必须大于零");
      return result;
    }
    const screenPolygon = outline2(polygon);
    if (!screenPolygon) {
      fail("invalid_polygon", "请在屏幕上画出有效闭合圈选");
      return result;
    }
    const flatProjection = ArrayBuffer.isView(projectedPoints) &&
      projectedPoints.length === count * 2;
    if (!flatProjection && (!Array.isArray(projectedPoints) || projectedPoints.length !== count)) {
      fail("invalid_projection", "每个 STL 三角面都需要一个屏幕面心坐标");
      return result;
    }
    let eligible = null;
    if (eligibleFaceIds != null) {
      if (!Array.isArray(eligibleFaceIds) && !(eligibleFaceIds instanceof Set)) {
        fail("invalid_eligible_faces", "候选面 ID 必须是数组或 Set");
        return result;
      }
      eligible = new Set(eligibleFaceIds);
      if ([...eligible].some((id) => !Number.isInteger(id) || id < 0 || id >= count)) {
        fail("invalid_eligible_faces", "候选面 ID 超出原始 STL 范围");
        return result;
      }
    }
    const face = (id) => [0, 3, 6].map((offset) => {
      const start = id * 9 + offset;
      return [positions[start], positions[start + 1], positions[start + 2]];
    });
    const vertexKey = (point) => point.map((value) =>
      Math.round(value / weldToleranceMm)).join(",");
    const keysByFace = new Map(), pointsByKey = new Map(), normalByFace = new Map();
    const edgeOwners = new Map();
    for (let id = 0; id < count; id += 1) {
      if (eligible && !eligible.has(id)) continue;
      const screen = flatProjection
        ? [projectedPoints[2 * id], projectedPoints[2 * id + 1]]
        : point2(projectedPoints[id]);
      if (!screen || !screen.every(Number.isFinite)) {
        fail("invalid_projection", "面 " + id + " 的屏幕坐标无效");
        return result;
      }
      if (!inPolygon(screen, screenPolygon)) continue;
      const triangle = face(id);
      if (triangle.some((point) => !point.every(Number.isFinite))) {
        fail("invalid_positions", "面 " + id + " 的三维坐标无效");
        return result;
      }
      const keys = triangle.map(vertexKey);
      if (new Set(keys).size !== 3) {
        fail("degenerate_face", "面 " + id + " 退化");
        return result;
      }
      keys.forEach((key, i) => { if (!pointsByKey.has(key)) pointsByKey.set(key, triangle[i]); });
      keysByFace.set(id, keys);
      normalByFace.set(id, cross3(subtract(triangle[1], triangle[0]),
        subtract(triangle[2], triangle[0])));
      for (let i = 0; i < 3; i += 1) {
        const key = edgeId(keys[i], keys[(i + 1) % 3]);
        if (!edgeOwners.has(key)) edgeOwners.set(key, []);
        edgeOwners.get(key).push(id);
      }
    }
    if (!keysByFace.size) {
      fail("no_faces", "圈选内没有三维网格面");
      return result;
    }
    if (seedFaceId != null && (!Number.isInteger(seedFaceId) || !keysByFace.has(seedFaceId))) {
      fail("seed_outside_selection", "入口定位点必须落在圈选的候选网格面上");
      return result;
    }
    const adjacent = new Map([...keysByFace.keys()].map((id) => [id, new Set()]));
    for (const owners of edgeOwners.values()) {
      if (owners.length > 2) fail("nonmanifold_selection", "圈选内存在非流形网格边");
      if (owners.length === 2) {
        adjacent.get(owners[0]).add(owners[1]);
        adjacent.get(owners[1]).add(owners[0]);
      }
    }
    if (quality_errors.length) return result;
    const components = [], remaining = new Set(keysByFace.keys());
    while (remaining.size) {
      const start = remaining.values().next().value, stack = [start], component = [];
      remaining.delete(start);
      while (stack.length) {
        const id = stack.pop();
        component.push(id);
        for (const next of adjacent.get(id)) if (remaining.delete(next)) stack.push(next);
      }
      components.push(component);
    }
    if (seedFaceId == null && components.length > 1) {
      fail("disconnected_regions", "圈选包含多个分离区域，请点选目标鼻孔面");
      return result;
    }
    const selected = seedFaceId == null ? components[0]
      : components.find((component) => component.includes(seedFaceId));
    if (components.length > 1) warnings.push({ code: "ignored_disconnected_regions",
      message: "圈选内其他不相连区域已排除" });
    selected.sort((a, b) => a - b);
    result.selected_face_ids = selected;
    result.seed_face_id = seedFaceId == null ? selected[0] : seedFaceId;
    const seedTriangle = face(seedFaceId == null ? selected[0] : seedFaceId);
    result.seed_lps_mm = seedTriangle.reduce((sum, point) =>
      sum.map((value, axis) => value + point[axis] / 3), [0, 0, 0]);
    const selectedEdges = new Map();
    for (const id of selected) {
      const keys = keysByFace.get(id);
      for (let i = 0; i < 3; i += 1) {
        const key = edgeId(keys[i], keys[(i + 1) % 3]);
        if (!selectedEdges.has(key)) selectedEdges.set(key,
          { a: keys[i], b: keys[(i + 1) % 3], count: 0 });
        selectedEdges.get(key).count += 1;
      }
    }
    if ([...selectedEdges.values()].some((edge) => edge.count > 2)) {
      fail("nonmanifold_selection", "选中区域存在非流形网格边");
      return result;
    }
    // Check selected edges against the source STL, without storing full-mesh adjacency.
    const sourceCounts = new Map([...selectedEdges.keys()].map((key) => [key, 0]));
    for (let id = 0; id < count; id += 1) {
      const keys = face(id).map(vertexKey);
      for (let i = 0; i < 3; i += 1) {
        const key = edgeId(keys[i], keys[(i + 1) % 3]);
        if (sourceCounts.has(key)) sourceCounts.set(key, sourceCounts.get(key) + 1);
      }
    }
    if ([...sourceCounts.values()].some((number) => number > 2))
      fail("nonmanifold_source", "原始 STL 在选中区域有非流形边");
    const border = [...selectedEdges.entries()].filter(([, edge]) => edge.count === 1);
    if (border.some(([key]) => sourceCounts.get(key) !== 2))
      fail("open_surface_boundary", "圈选边界碰到原始 STL 的开放边");
    if (!border.length) fail("no_boundary", "选中区域没有可封口的边界");
    if (quality_errors.length) return result;
    const neighbors = new Map();
    for (const [, edge] of border) {
      if (!neighbors.has(edge.a)) neighbors.set(edge.a, []);
      if (!neighbors.has(edge.b)) neighbors.set(edge.b, []);
      neighbors.get(edge.a).push(edge.b);
      neighbors.get(edge.b).push(edge.a);
    }
    if ([...neighbors.values()].some((items) => items.length !== 2)) {
      fail("open_boundary", "圈选区域边界未形成简单闭合环");
      return result;
    }
    const unwalked = new Set(border.map(([key]) => key)), loops = [];
    while (unwalked.size) {
      const first = border.find(([key]) => unwalked.has(key))[1];
      const start = first.a, loop = [start];
      let previous = null, current = start;
      do {
        const next = neighbors.get(current).find((other) => other !== previous &&
          unwalked.has(edgeId(current, other)));
        if (next == null) { fail("open_boundary", "圈选区域边界未闭合"); return result; }
        unwalked.delete(edgeId(current, next));
        previous = current; current = next;
        if (current !== start) loop.push(current);
      } while (current !== start);
      loops.push(loop);
    }
    if (loops.length !== 1) {
      fail("multiple_boundaries", "圈选区域形成多个边界环，不能作为一个鼻孔端口");
      return result;
    }
    let boundary = loops[0].map((key) => pointsByKey.get(key));
    if (boundary.length < 3) { fail("degenerate_boundary", "开口边界点不足"); return result; }
    let vectorArea = [0, 0, 0];
    for (let i = 0; i < boundary.length; i += 1) {
      const value = cross3(boundary[i], boundary[(i + 1) % boundary.length]);
      vectorArea = vectorArea.map((number, axis) => number + value[axis]);
    }
    const magnitude = Math.hypot(...vectorArea), area = magnitude / 2;
    if (area <= 1e-6) { fail("degenerate_boundary", "开口边界面积为零"); return result; }
    let normal = vectorArea.map((value) => value / magnitude);
    const patchNormal = selected.reduce((sum, id) =>
      sum.map((value, axis) => value + normalByFace.get(id)[axis]), [0, 0, 0]);
    if (dot(normal, patchNormal) < 0) {
      boundary = boundary.slice().reverse();
      normal = normal.map((value) => -value);
    }
    const mean = boundary.reduce((sum, point) =>
      sum.map((value, axis) => value + point[axis] / boundary.length), [0, 0, 0]);
    const deviation = Math.max(...boundary.map((point) =>
      Math.abs(dot(subtract(point, mean), normal))));
    let weighted = [0, 0, 0], signedArea = 0;
    for (let i = 1; i < boundary.length - 1; i += 1) {
      const triangleArea = dot(cross3(subtract(boundary[i], boundary[0]),
        subtract(boundary[i + 1], boundary[0])), normal) / 2;
      signedArea += triangleArea;
      weighted = weighted.map((value, axis) => value + triangleArea *
        (boundary[0][axis] + boundary[i][axis] + boundary[i + 1][axis]) / 3);
    }
    if (Math.abs(signedArea) <= 1e-6) {
      fail("degenerate_boundary", "开口边界无法计算面积中心");
      return result;
    }
    const centroid = weighted.map((value) => value / signedArea);
    result.boundary_loop_lps_mm = [...boundary, boundary[0]].map((point) => point.slice());
    result.centroid_lps_mm = centroid;
    result.area_mm2 = area;
    result.normal_lps = normal;
    result.plane = { point_lps_mm: centroid.slice(), normal_lps: normal.slice() };
    result.max_plane_deviation_mm = deviation;
    if (deviation > planeToleranceMm)
      fail("nonplanar_boundary", "入口边界不在同一切面，不能生成单一平面封口");
    result.ok = quality_errors.length === 0;
    return result;
  }

  // Depth visibility at each original face centroid. Screen XY and NDC Z must
  // come from the same camera projection; smaller Z is closer to the viewer.
  function visibleFaceIds({ projectedTriangles, projectedPoints, polygon,
    depthTolerance = 1e-5 } = {}) {
    const quality_errors = [], eligibleFaceIds = [];
    const result = { eligibleFaceIds, quality_errors };
    const fail = (code, message) => quality_errors.push({ code, message });
    const flatPoints = ArrayBuffer.isView(projectedPoints);
    const count = flatPoints ? projectedPoints.length / 2
      : Array.isArray(projectedPoints) ? projectedPoints.length : NaN;
    const flatTriangles = ArrayBuffer.isView(projectedTriangles);
    if (!Number.isInteger(count) || count < 1 ||
        flatTriangles && projectedTriangles.length !== count * 9 ||
        !flatTriangles && (!Array.isArray(projectedTriangles) || projectedTriangles.length !== count)) {
      fail("invalid_projection", "需要每个原始 STL 面的屏幕顶点和面心投影");
      return result;
    }
    const lasso = outline2(polygon);
    if (!lasso) { fail("invalid_polygon", "屏幕圈选不是简单闭合多边形"); return result; }
    if (!Number.isFinite(depthTolerance) || depthTolerance < 0) {
      fail("invalid_tolerance", "深度容差不能为负数");
      return result;
    }
    const triangleAt = (id) => flatTriangles
      ? [0, 3, 6].map((offset) => Array.from(projectedTriangles.slice(id * 9 + offset,
        id * 9 + offset + 3)))
      : projectedTriangles[id];
    const screenAt = (id) => flatPoints
      ? [projectedPoints[id * 2], projectedPoints[id * 2 + 1]]
      : point2(projectedPoints[id]);
    const limits = [Math.min(...lasso.map((point) => point[0])),
      Math.max(...lasso.map((point) => point[0])),
      Math.min(...lasso.map((point) => point[1])),
      Math.max(...lasso.map((point) => point[1]))];
    const cellSize = Math.max(4, (limits[1] - limits[0]) / 64,
      (limits[3] - limits[2]) / 64);
    const columns = Math.max(1, Math.ceil((limits[1] - limits[0]) / cellSize));
    const rows = Math.max(1, Math.ceil((limits[3] - limits[2]) / cellSize));
    const column = (x) => Math.max(0, Math.min(columns - 1,
      Math.floor((x - limits[0]) / cellSize)));
    const row = (y) => Math.max(0, Math.min(rows - 1,
      Math.floor((y - limits[2]) / cellSize)));
    const tiles = new Map(), triangles = new Map();
    for (let id = 0; id < count; id += 1) {
      const triangle = triangleAt(id);
      if (!Array.isArray(triangle) || triangle.length !== 3 ||
          triangle.some((point) => !Array.isArray(point) || point.length !== 3 ||
            !point.every(Number.isFinite))) {
        fail("invalid_projection", "面 " + id + " 的投影顶点无效");
        return result;
      }
      const xs = triangle.map((point) => point[0]);
      const ys = triangle.map((point) => point[1]);
      const bbox = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
      if (bbox[1] < limits[0] || bbox[0] > limits[1] ||
          bbox[3] < limits[2] || bbox[2] > limits[3]) continue;
      const depths = triangle.map((point) => point[2]);
      if (depths.every((depth) => depth < -1) || depths.every((depth) => depth > 1)) continue;
      if (depths.some((depth) => depth < -1 || depth > 1)) {
        fail("clip_required", "三角面跨越相机近远裁剪面，不能可靠判断可见性");
        return result;
      }
      triangles.set(id, triangle);
      for (let y = row(bbox[2]); y <= row(bbox[3]); y += 1) {
        for (let x = column(bbox[0]); x <= column(bbox[1]); x += 1) {
          const key = y * columns + x;
          if (!tiles.has(key)) tiles.set(key, []);
          tiles.get(key).push(id);
        }
      }
    }
    const depthAt = (triangle, point) => {
      const [a, b, c] = triangle;
      const denominator = (b[1] - c[1]) * (a[0] - c[0]) +
        (c[0] - b[0]) * (a[1] - c[1]);
      if (Math.abs(denominator) <= 1e-10) return null;
      const wa = ((b[1] - c[1]) * (point[0] - c[0]) +
        (c[0] - b[0]) * (point[1] - c[1])) / denominator;
      const wb = ((c[1] - a[1]) * (point[0] - c[0]) +
        (a[0] - c[0]) * (point[1] - c[1])) / denominator;
      const wc = 1 - wa - wb;
      if (Math.min(wa, wb, wc) < -1e-7) return null;
      return wa * a[2] + wb * b[2] + wc * c[2];
    };
    for (let id = 0; id < count; id += 1) {
      const screen = screenAt(id);
      if (!screen || !screen.every(Number.isFinite)) {
        fail("invalid_projection", "面 " + id + " 的屏幕面心无效");
        eligibleFaceIds.length = 0;
        return result;
      }
      if (!inPolygon(screen, lasso) || !triangles.has(id)) continue;
      const ownDepth = depthAt(triangles.get(id), screen);
      if (ownDepth == null) continue;
      const candidates = tiles.get(row(screen[1]) * columns + column(screen[0])) || [];
      let nearestDepth = Infinity;
      for (const other of candidates) {
        const depth = depthAt(triangles.get(other), screen);
        if (depth != null && depth < nearestDepth) nearestDepth = depth;
      }
      if (ownDepth <= nearestDepth + depthTolerance) eligibleFaceIds.push(id);
    }
    return result;
  }

  return { empty, normalize, withSeed, withOutline, summary, buildPortPatch, visibleFaceIds };
});
