import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

test("manifest declares the dsh bundle and client entries", () => {
  const pkg = JSON.parse(read("package.json"));
  assert.equal(pkg.name, "econtone-studio");
  assert.equal(pkg.main, "lib/index.js");
  assert.ok(pkg.dsh && pkg.dsh.bundle && pkg.dsh.bundle.patch);
  assert.ok(pkg.dsh.client && pkg.dsh.client.platform === "web");
  assert.ok(pkg.engines && pkg.engines.dsh, "engines.dsh is required for the compatibility signal");
  assert.ok(Array.isArray(pkg.files) && pkg.files.includes("lib/**"));
});

test("built host and client exist in lib/", () => {
  for (const f of ["lib/index.js", "lib/client.js", "lib/standalone.html", "lib/cordis.patch.yml", "lib/engine/studio_bridge.py"]) {
    assert.ok(fs.existsSync(path.join(ROOT, f)), `${f} missing — run python build/build_lib.py`);
  }
});

test("client bundle registers a module and stays scoped", () => {
  const js = read("lib/client.js");
  assert.match(js, /__ModuleLoader__/);
  assert.match(js, /\.ets-root/);
});

test("host server registers the studio routes", () => {
  const js = read("lib/index.js");
  assert.match(js, /webServer\.register/);
  for (const route of ["/run", "/state", "/history", "/skills"]) {
    assert.ok(js.includes(`'${route}'`), `route ${route} missing`);
  }
});

test("engine bridge exposes the CLI verbs the host calls", () => {
  const py = read("lib/engine/studio_bridge.py");
  for (const verb of ["health", "measure", "arbitrate"]) {
    assert.ok(py.includes(verb), `bridge verb ${verb} missing`);
  }
});
