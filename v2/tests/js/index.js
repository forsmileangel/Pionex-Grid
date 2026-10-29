// Node 24 treats `node --test <dir>` as a glob for that path, not as a
// recursive search. This entry lets that command run the files in this folder.
const { spawnSync } = require("node:child_process");
const path = require("node:path");

const root = path.resolve(__dirname, "../../..");
const env = { ...process.env };
delete env.NODE_TEST_CONTEXT;
const runner = spawnSync(process.execPath, ["--test", "v2/tests/js/*.test.mjs"], {
  stdio: "inherit",
  cwd: root,
  env,
});
process.exit(runner.status === null ? 1 : runner.status);
