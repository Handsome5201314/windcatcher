/* Geometry shared by the endplane editor and its regression tests. LPS, mm. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory;
  else root.EndplaneTools = factory(root.THREE);
})(typeof window === "undefined" ? this : window, function (THREE) {
  "use strict";

  function basis(normal) {
    const n = new THREE.Vector3(...normal).normalize();
    const up = Math.abs(n.z) < 0.9 ? new THREE.Vector3(0, 0, 1) : new THREE.Vector3(0, 1, 0);
    const u = up.cross(n).normalize();
    return { n, u, v: n.clone().cross(u).normalize() };
  }

  function throughPoints(points, outward) {
    if (points.length !== 3) return null;
    const [a, b, c] = points.map((point) => new THREE.Vector3(...point));
    const ab = b.clone().sub(a), ac = c.clone().sub(a);
    const normal = ab.clone().cross(ac);
    // Reject coincident and nearly collinear picks, rather than inventing a plane.
    if (ab.length() < 0.5 || ac.length() < 0.5 || normal.length() < ab.length() * ac.length() * 0.05) return null;
    normal.normalize();
    if (normal.dot(new THREE.Vector3(...outward)) < 0) normal.negate();
    return { point_lps_mm: a.add(b).add(c).multiplyScalar(1 / 3).toArray(), normal_lps: normal.toArray() };
  }

  function adjustedPlane(anchor, offset, tiltU, tiltV) {
    const { n, u, v } = basis(anchor.normal_lps);
    const radians = Math.PI / 180;
    n.applyAxisAngle(u, tiltU * radians).applyAxisAngle(v, tiltV * radians).normalize();
    const point = new THREE.Vector3(...anchor.point_lps_mm).addScaledVector(n, offset);
    return { point_lps_mm: point.toArray(), normal_lps: n.toArray() };
  }

  function intersectionSegments(geometry, plane, peer = null) {
    const positions = geometry.getAttribute("position"), indices = geometry.index;
    const count = indices ? indices.count : positions.count;
    const normal = new THREE.Vector3(...plane.normal_lps).normalize();
    const constant = normal.dot(new THREE.Vector3(...plane.point_lps_mm));
    const peerNormal = peer ? new THREE.Vector3(...peer.normal_lps).normalize() : null;
    const peerConstant = peer ? peerNormal.dot(new THREE.Vector3(...peer.point_lps_mm)) : 0;
    const vertices = [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()];
    const result = [], epsilon = 1e-5;
    for (let triangle = 0; triangle + 2 < count; triangle += 3) {
      const distances = vertices.map((vertex, j) => {
        vertex.fromBufferAttribute(positions, indices ? indices.getX(triangle + j) : triangle + j);
        return normal.dot(vertex) - constant;
      });
      if (distances.every((d) => d > epsilon) || distances.every((d) => d < -epsilon) || distances.every((d) => Math.abs(d) <= epsilon)) continue;
      const hits = [];
      for (let j = 0; j < 3; j += 1) {
        const next = (j + 1) % 3, da = distances[j], db = distances[next];
        let hit = null;
        if (Math.abs(da) <= epsilon) hit = vertices[j].clone();
        else if ((da < 0) !== (db < 0)) hit = vertices[j].clone().lerp(vertices[next], da / (da - db));
        if (hit && !hits.some((other) => other.distanceToSquared(hit) < epsilon * epsilon)) hits.push(hit);
      }
      if (hits.length !== 2) continue;
      if (peerNormal) {
        const a = peerNormal.dot(hits[0]) - peerConstant, b = peerNormal.dot(hits[1]) - peerConstant;
        if (a > epsilon && b > epsilon) continue;
        if (a > epsilon) hits[0].lerp(hits[1], a / (a - b));
        else if (b > epsilon) hits[1].lerp(hits[0], b / (b - a));
      }
      if (hits[0].distanceToSquared(hits[1]) > epsilon * epsilon) result.push(...hits[0].toArray(), ...hits[1].toArray());
    }
    return result;
  }

  function nearestIntersectionComponent(segments, point, tolerance = 1e-3) {
    if (!Array.isArray(segments) || segments.length < 6 || segments.length % 6 !== 0 ||
        !Array.isArray(point) || point.length !== 3 || !point.every(Number.isFinite)) return [];
    const weldTolerance = Number.isFinite(tolerance) ? Math.min(0.1, Math.max(1e-5, tolerance)) : 1e-3;
    const segmentCount = segments.length / 6;
    const points = Array.from({ length: segmentCount * 2 }, (_, index) =>
      segments.slice(index * 3, index * 3 + 3));
    const parent = Int32Array.from({ length: points.length }, (_, index) => index);
    const find = (value) => {
      let root = value;
      while (parent[root] !== root) root = parent[root];
      while (parent[value] !== value) {
        const next = parent[value];
        parent[value] = root;
        value = next;
      }
      return root;
    };
    const unite = (first, second) => {
      const a = find(first), b = find(second);
      if (a !== b) parent[b] = a;
    };
    const buckets = new Map();
    const key = (x, y, z) => String(x) + "," + String(y) + "," + String(z);
    const squaredTolerance = weldTolerance * weldTolerance;
    for (let index = 0; index < points.length; index += 1) {
      const current = points[index];
      const cell = current.map((value) => Math.floor(value / weldTolerance));
      for (let dx = -1; dx <= 1; dx += 1) for (let dy = -1; dy <= 1; dy += 1) for (let dz = -1; dz <= 1; dz += 1) {
        for (const candidate of buckets.get(key(cell[0] + dx, cell[1] + dy, cell[2] + dz)) || []) {
          const other = points[candidate];
          const distance = current.reduce((sum, value, axis) => sum + (value - other[axis]) ** 2, 0);
          if (distance <= squaredTolerance) unite(index, candidate);
        }
      }
      const bucketKey = key(...cell);
      const bucket = buckets.get(bucketKey) || [];
      bucket.push(index);
      buckets.set(bucketKey, bucket);
    }
    // Join every segment before grouping: later edges may connect earlier roots.
    for (let segment = 0; segment < segmentCount; segment += 1) unite(segment * 2, segment * 2 + 1);
    const components = new Map();
    for (let segment = 0; segment < segmentCount; segment += 1) {
      const root = find(segment * 2);
      const component = components.get(root) || [];
      component.push(segment);
      components.set(root, component);
    }
    const distanceToSegment = (segment) => {
      const first = segments.slice(segment * 6, segment * 6 + 3);
      const second = segments.slice(segment * 6 + 3, segment * 6 + 6);
      const direction = second.map((value, axis) => value - first[axis]);
      const lengthSquared = direction.reduce((sum, value) => sum + value * value, 0);
      const projection = lengthSquared > 0
        ? Math.max(0, Math.min(1, direction.reduce((sum, value, axis) => sum + (point[axis] - first[axis]) * value, 0) / lengthSquared))
        : 0;
      return first.reduce((sum, value, axis) => sum + (point[axis] - value - projection * direction[axis]) ** 2, 0);
    };
    let nearest = null, nearestDistance = Infinity;
    for (const component of components.values()) {
      const distance = Math.min(...component.map(distanceToSegment));
      if (distance < nearestDistance) { nearest = component; nearestDistance = distance; }
    }
    return nearest ? nearest.flatMap((segment) => segments.slice(segment * 6, segment * 6 + 6)) : [];
  }

  function sectionTriangles(segments, normal, tolerance = 1e-3) {
    if (!Array.isArray(segments) || segments.length < 6 || segments.length % 6 !== 0 ||
        !Array.isArray(normal) || normal.length !== 3 || !normal.every(Number.isFinite)) return [];
    const weldTolerance = Number.isFinite(tolerance) ? Math.min(0.1, Math.max(1e-5, tolerance)) : 1e-3;
    const squaredTolerance = weldTolerance * weldTolerance;
    const frame = basis(normal);
    const vertices = [];
    const buckets = new Map();
    const edges = [];
    const edgeKeys = new Set();
    const cellKey = (x, y, z) => `${x},${y},${z}`;
    const vertexId = (values) => {
      const point = new THREE.Vector3(...values);
      const cell = values.map((value) => Math.floor(value / weldTolerance));
      for (let dx = -1; dx <= 1; dx += 1) for (let dy = -1; dy <= 1; dy += 1) for (let dz = -1; dz <= 1; dz += 1) {
        for (const candidate of buckets.get(cellKey(cell[0] + dx, cell[1] + dy, cell[2] + dz)) || []) {
          if (vertices[candidate].point.distanceToSquared(point) <= squaredTolerance) return candidate;
        }
      }
      const id = vertices.length;
      vertices.push({ point, edges: [] });
      const key = cellKey(...cell);
      const bucket = buckets.get(key) || [];
      bucket.push(id);
      buckets.set(key, bucket);
      return id;
    };
    for (let offset = 0; offset < segments.length; offset += 6) {
      const first = vertexId(segments.slice(offset, offset + 3));
      const second = vertexId(segments.slice(offset + 3, offset + 6));
      if (first === second) continue;
      const key = first < second ? `${first}:${second}` : `${second}:${first}`;
      if (edgeKeys.has(key)) continue;
      edgeKeys.add(key);
      const edge = edges.length;
      edges.push([first, second]);
      vertices[first].edges.push(edge);
      vertices[second].edges.push(edge);
    }

    const visited = new Set();
    const triangles = [];
    for (let start = 0; start < vertices.length; start += 1) {
      if (!vertices[start].edges.length || visited.has(start)) continue;
      const component = [];
      const stack = [start];
      visited.add(start);
      while (stack.length) {
        const current = stack.pop();
        component.push(current);
        for (const edge of vertices[current].edges) {
          const next = edges[edge][0] === current ? edges[edge][1] : edges[edge][0];
          if (!visited.has(next)) { visited.add(next); stack.push(next); }
        }
      }
      if (component.some((id) => vertices[id].edges.length !== 2)) continue;
      const loop = [];
      const usedEdges = new Set();
      let current = start, previousEdge = -1, closed = false;
      for (let step = 0; step <= component.length; step += 1) {
        loop.push(current);
        const edge = vertices[current].edges.find((candidate) => candidate !== previousEdge);
        if (edge === undefined || usedEdges.has(edge)) break;
        usedEdges.add(edge);
        const next = edges[edge][0] === current ? edges[edge][1] : edges[edge][0];
        if (next === start) { closed = true; break; }
        previousEdge = edge;
        current = next;
      }
      if (!closed || usedEdges.size !== component.length || loop.length < 3) continue;
      const points = loop.map((id) => vertices[id].point);
      const contour = points.map((point) => new THREE.Vector2(point.dot(frame.u), point.dot(frame.v)));
      if (Math.abs(THREE.ShapeUtils.area(contour)) <= squaredTolerance) continue;
      for (const triangle of THREE.ShapeUtils.triangulateShape(contour, [])) {
        for (const index of triangle) triangles.push(...points[index].toArray());
      }
    }
    return triangles;
  }

  return { basis, throughPoints, adjustedPlane, intersectionSegments, nearestIntersectionComponent, sectionTriangles };
});
