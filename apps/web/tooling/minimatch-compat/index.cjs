/* eslint-disable @typescript-eslint/no-require-imports -- CommonJS is the legacy consumer API under test. */
// Legacy ESLint plugins require a callable export; all matching uses patched v11.
function callable(api) {
  const match = (path, pattern, options) => api(path, pattern, options);
  return Object.assign(match, api, {
    minimatch: match,
    // eslint-plugin-import consumes only .test(), not a RegExp. Keep matching
    // on v11's nonrecursive engine instead of rebuilding the removed regex API.
    makeRe: (pattern, options) => {
      const matcher = new api.Minimatch(pattern, options);
      return { test: (path) => matcher.match(path) };
    },
    defaults: (options) => callable(api.defaults(options)),
  });
}

module.exports = callable(require("minimatch-engine").minimatch);
