/* eslint-disable @typescript-eslint/no-require-imports -- CommonJS is the legacy consumer API under test. */
const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const { test } = require("node:test");
const { ESLint } = require("eslint");

const legacy = createRequire(require.resolve("eslint-plugin-react"))("minimatch");

test("legacy callable, constructors and glob options", () => {
  assert.equal(legacy("src/page.tsx", "**/*.{ts,tsx}"), true);
  assert.equal(legacy("src/.hidden.ts", "**/*.ts"), false);
  assert.equal(legacy("src/.hidden.ts", "**/*.ts", { dot: true }), true);
  assert.equal(legacy("src/PAGE.TSX", "*.tsx", { matchBase: true, nocase: true }), true);
  assert.equal(legacy("src/page.js", "**/*.@(ts|tsx)"), false);
  assert.equal(new legacy.Minimatch("**/*.tsx").match("src/page.tsx"), true);
  assert.equal(legacy.makeRe("**/*.tsx").test("src/page.tsx"), true);
  assert.deepEqual(legacy.braceExpand("src/*.{ts,tsx}"), ["src/*.ts", "src/*.tsx"]);
  assert.deepEqual(legacy.match(["a.ts", "b.js"], "*.ts"), ["a.ts"]);
  assert.deepEqual(["a.ts", "b.js"].filter(legacy.filter("*.ts")), ["a.ts"]);
});

test("defaults keep the callable API, makeRe and per-call overrides", () => {
  const match = legacy.defaults({ nocase: true });
  assert.equal(match("FILE.TS", "*.ts"), true);
  assert.equal(match("FILE.TS", "*.ts", { nocase: false }), false);
  assert.equal(match.makeRe("*.ts").test("FILE.TS"), true);
  assert.equal(new match.Minimatch("*.ts").match("FILE.TS"), true);
  assert.equal(match.defaults({ dot: true })(".FILE.TS", "*.ts"), true);
});

async function lint(rule, options, source, pluginName, moduleName) {
  const eslint = new ESLint({
    overrideConfigFile: true,
    overrideConfig: [{
      files: ["**/*.jsx"],
      languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
      plugins: { [pluginName]: require(moduleName) },
      rules: { [rule]: ["error", options] },
    }],
  });
  const [result] = await eslint.lintText(source, { filePath: "compat.jsx" });
  assert.equal(result.fatalErrorCount, 0);
  return result.messages.map((message) => message.ruleId);
}

test("React plugin still applies wildcard component exceptions", async () => {
  const source = "const Demo = () => <HELLO_WORLD />;";
  assert.deepEqual(await lint("react/jsx-pascal-case", {}, source,
    "react", "eslint-plugin-react"), ["react/jsx-pascal-case"]);
  assert.deepEqual(await lint("react/jsx-pascal-case", { ignore: ["HELLO_*"] }, source,
    "react", "eslint-plugin-react"), []);
});

test("accessibility plugin still recognizes wildcard control components", async () => {
  const source = "const Demo = () => <label>Name<CustomInput /></label>;";
  assert.deepEqual(await lint("jsx-a11y/label-has-associated-control", {}, source,
    "jsx-a11y", "eslint-plugin-jsx-a11y"), ["jsx-a11y/label-has-associated-control"]);
  assert.deepEqual(await lint("jsx-a11y/label-has-associated-control",
    { controlComponents: ["Custom*"] }, source, "jsx-a11y", "eslint-plugin-jsx-a11y"), []);
});

test("import plugin still orders paths matched by a glob", async () => {
  assert.deepEqual(await lint("import/order", {
    groups: ["builtin", "internal"],
    pathGroups: [{ pattern: "@app/**", group: "internal" }],
  }, "import x from '@app/x'; import fs from 'node:fs';",
  "import", "eslint-plugin-import"), ["import/order"]);
});

test("import plugin still checks allowed and forbidden internal paths", async () => {
  const source = "import x from 'pkg/private/x';";
  assert.deepEqual(await lint("import/no-internal-modules",
    { allow: ["pkg/**"] }, source, "import", "eslint-plugin-import"), []);
  assert.deepEqual(await lint("import/no-internal-modules",
    { forbid: ["pkg/private/**"] }, source, "import", "eslint-plugin-import"),
  ["import/no-internal-modules"]);
});

test("ESM default import still exposes the callable API", async () => {
  const { pathToFileURL } = require("node:url");
  const entry = createRequire(require.resolve("eslint-plugin-react")).resolve("minimatch");
  const { default: match } = await import(pathToFileURL(entry).href);
  assert.equal(match("src/file.ts", "**/*.ts"), true);
});
