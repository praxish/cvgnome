// SPDX-License-Identifier: MPL-2.0
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
function run(command, args, capture = false) {
  const result = spawnSync(command, args, {
    cwd: root, encoding: "utf8", stdio: capture ? "pipe" : "inherit",
    shell: process.platform === "win32",
  });
  if (result.error || result.status !== 0) {
    throw new Error(`${command} failed: ${result.error?.message ?? result.stderr ?? result.status}`);
  }
  return result.stdout?.trim();
}

const nodeVersion = readFileSync(new URL("../.node-version", import.meta.url), "utf8").trim();
const uvVersion = readFileSync(new URL("../.uv-version", import.meta.url), "utf8").trim();
if (process.versions.node !== nodeVersion) {
  throw new Error(`Use Node ${nodeVersion} from .node-version (found ${process.versions.node})`);
}
if (run("uv", ["--version"], true).split(/\s+/)[1] !== uvVersion) {
  throw new Error(`Install uv ${uvVersion} from .uv-version`);
}
run("npm", ["ci"]);
run("uv", ["sync", "--locked", "--project", "engine", "--group", "dev"]);
const target = run("rustc", ["--print", "host-tuple"], true);
run("cargo", ["fetch", "--locked", "--manifest-path", "src-tauri/Cargo.toml", "--target", target]);
console.log(`Locked dependencies installed for ${target}. Run npm run check.`);
