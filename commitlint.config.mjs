const commitlintConfig = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "body-leading-blank": [2, "always"],
    "body-max-line-length": [2, "always", 100],
    "footer-leading-blank": [2, "always"],
    "header-max-length": [2, "always", 72],
    "scope-case": [2, "always", "lower-case"],
    "scope-empty": [2, "never"],
    "scope-enum": [
      2,
      "always",
      [
        "ci",
        "cli",
        "controller",
        "decide",
        "deps",
        "docs",
        "inventory",
        "log",
        "model",
        "play",
        "reconcile",
        "repo",
        "scenarios",
        "stubs",
        "tests",
        "world",
      ],
    ],
    "subject-full-stop": [2, "never", "."],
    "trailer-exists": [2, "never", "Co-authored-by:"],
    "type-enum": [
      2,
      "always",
      ["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"],
    ],
  },
};

export default commitlintConfig;
