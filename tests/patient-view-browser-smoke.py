"""Local-only WebGL/mobile acceptance; synthetic by default, never starts CFD.

Run with a Python environment containing Playwright. --model accepts an already
authorized, sanitized patient view, not CT or a research result payload.
"""
import argparse
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".local-public" / "legend-ui-smoke"
ASSETS = {"/static/" + path for path in ("patient-view.css", "patient-view.js", "reference-view.js",
          "cfd-viewer.js", "vendor/three.min.js", "vendor/OrbitControls.js")}


def synthetic_model():
    positions, indices, pressure, streamlines = [], [], [], []
    for side in (-1, 1):
        points, speeds, values = [], [], []
        start = len(positions) // 3
        for ring in range(36):
            t = ring / 35
            x, y, z = 0.18 + side * 0.009, 0.025 + t * 0.08, -0.015 + math.sin(t * math.pi) * 0.025
            p = (23.14 - t * 24.72) * 1.2
            points.extend((x, y, z))
            speeds.append(0.6 + math.sin(t * math.pi) * 3.6)
            values.append(p)
            for corner in range(16):
                angle = corner * math.tau / 16
                positions.extend((x + 0.005 * math.cos(angle), y, z + 0.006 * math.sin(angle)))
                pressure.append(p)
                if ring:
                    a, b = start + (ring - 1) * 16 + corner, start + (ring - 1) * 16 + (corner + 1) % 16
                    indices.extend((a, b, a + 16, b, b + 16, a + 16))
        streamlines.append({"positions": points, "speeds": speeds, "pressure": values})
    axes = [positions[i::3] for i in range(3)]
    return {"units": {"coordinates": "m", "velocity": "m/s", "pressure": "Pa"},
            "bounds_m": [v for axis in axes for v in (min(axis), max(axis))],
            "wall": {"positions": positions, "indices": indices, "pressure": pressure},
            "patches": {}, "slices": [], "streamlines": streamlines,
            "ranges": {"velocity": [0.6, 4.2], "pressure": [-1.896, 27.768]}}


CAPTURE_VIEWER = """
Object.defineProperty(window, 'CfdViewer', { configurable: true, set(Viewer) {
  Object.defineProperty(window, 'CfdViewer', { configurable: true, writable: true,
    value: new Proxy(Viewer, { construct(target, args) {
      const instance = Reflect.construct(target, args);
      window.__qaViewer = instance;
      return instance;
    } })
  });
} });
"""

FRAME_METRICS = """() => {
  const v = window.__qaViewer;
  v.render();
  v.camera.updateMatrixWorld();
  const bounds = new THREE.Box3().setFromObject(v.wall);
  bounds.expandByObject(v.patches);
  bounds.expandByObject(v.lineGroup);
  let maxX = 0, maxY = 0;
  for (const x of [bounds.min.x, bounds.max.x])
    for (const y of [bounds.min.y, bounds.max.y])
      for (const z of [bounds.min.z, bounds.max.z]) {
        const p = new THREE.Vector3(x, y, z).project(v.camera);
        maxX = Math.max(maxX, Math.abs(p.x));
        maxY = Math.max(maxY, Math.abs(p.y));
      }
  const center = v.frameCenter.clone().project(v.camera);
  return { maxX, maxY, centerX: center.x, centerY: center.y,
    width: innerWidth, scrollWidth: document.documentElement.scrollWidth };
}"""


def run(model, supplied):
    template = (ROOT / "deploy/patient-view.html").read_text(encoding="utf-8")
    for marker, value in {"TITLE": "界面验收模型", "INTRO": "仅限本机的浏览器显示验收",
                          "WARNING": "", "MODEL_WARNING": ""}.items():
        template = template.replace("<!--SHARE_" + marker + "-->", value)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/qa/pa", "/qa/legacy"):
                body, content_type = template.encode(), "text/html; charset=utf-8"
            elif path in ("/qa/pa/model", "/qa/legacy/model"):
                data = json.loads(json.dumps(model))
                if path.startswith("/qa/legacy"):
                    data["units"]["pressure"] = "m2/s2"
                body, content_type = json.dumps(data).encode(), "application/json"
            elif path in ASSETS:
                body = (ROOT / path.lstrip("/")).read_bytes()
                content_type = "text/css" if path.endswith(".css") else "text/javascript"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass  # Local acceptance requests contain no useful diagnostic detail.

    OUTPUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {"source": "authorized patient snapshot" if supplied else "synthetic", "views": []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for width, height in ((320, 740), (390, 844), (844, 390), (1440, 1000)):
                    context = browser.new_context(viewport={"width": width, "height": height},
                                                  reduced_motion="reduce", has_touch=width < 1000)
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.add_init_script(CAPTURE_VIEWER)
                    page.goto(f"http://127.0.0.1:{server.server_port}/qa/pa")
                    expect(page.locator("#model canvas")).to_be_attached()
                    expect(page.locator("#legend-title")).to_have_text("速度颜色说明")
                    expect(page.locator("#legend-description")).to_contain_text("不代表正常或异常")
                    page.locator("#presentation-mode").select_option("flow")
                    page.locator("#field").select_option("pressure")
                    expect(page.locator("#legend-title")).to_have_text("压力颜色说明")
                    expect(page.locator("#legend-max")).to_contain_text("Pa" if model["units"]["pressure"] == "Pa" else "m²/s²")
                    assert page.locator("#legend-colorbar").evaluate("el => getComputedStyle(el).backgroundImage").startswith("linear-gradient(")
                    initial = page.evaluate(FRAME_METRICS)
                    assert initial["scrollWidth"] <= width, initial
                    assert initial["maxX"] < 0.94 and initial["maxY"] < 0.94, initial
                    assert abs(initial["centerX"]) < 1e-6 and abs(initial["centerY"]) < 1e-6, initial
                    for azimuth in (-1.1, 0, 1.5, 3.1, 5.4):
                        page.evaluate("a => window.__qaViewer.setPresentationPose(a, 0.32)", azimuth)
                        frame = page.evaluate(FRAME_METRICS)
                        assert frame["maxX"] < 0.94 and frame["maxY"] < 0.94, frame
                    page.locator("#view-reset").click()
                    reset = page.evaluate(FRAME_METRICS)
                    assert abs(reset["centerX"]) < 1e-6 and abs(reset["centerY"]) < 1e-6, reset
                    page.screenshot(path=str(OUTPUT / f"pressure-{width}.png"), full_page=True)
                    page.locator("#tour-toggle").click()
                    expect(page.locator("#legend-title")).to_have_text("速度颜色说明")
                    page.goto(f"http://127.0.0.1:{server.server_port}/qa/legacy")
                    page.locator("#presentation-mode").select_option("flow")
                    page.locator("#field").select_option("pressure")
                    expect(page.locator("#legend-unit-note")).to_contain_text("未换算为 Pa")
                    assert not errors, errors
                    report["views"].append({"viewport": [width, height], "initial": initial,
                                            "checked_orbit_poses": 5, "errors": errors})
                    context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    (OUTPUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    run(json.loads(args.model.read_text(encoding="utf-8")) if args.model else synthetic_model(), bool(args.model))
