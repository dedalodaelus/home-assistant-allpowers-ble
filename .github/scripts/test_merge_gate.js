"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const workflowPath = path.join(__dirname, "..", "workflows", "merge-gate.yml");
const workflow = fs.readFileSync(workflowPath, "utf8");
const workflowLines = workflow.split(/\r?\n/);
const scriptLine = workflowLines.findIndex((line) => line.trim() === "script: |");
assert.notEqual(scriptLine, -1, "merge gate must retain its executable GitHub Script body");
const scriptLines = [];
for (const line of workflowLines.slice(scriptLine + 1)) {
  if (!line.trim()) {
    scriptLines.push("");
  } else if (line.startsWith("            ")) {
    scriptLines.push(line.slice(12));
  } else {
    break;
  }
}
const runWorkflowScript = new AsyncFunction(
  "github",
  "context",
  "core",
  "process",
  "Buffer",
  "Date",
  "setTimeout",
  scriptLines.join("\n"),
);

const CHECK_NAMES = [
  "Python, tests, and release validation",
  "HACS validation",
  "Home Assistant Hassfest",
  "Validate title",
  "Review dependency changes",
  "Analyze Python",
];

function heading(version) {
  return `## [${version}](https://example.test/compare/previous...${version})`;
}

function fixture({
  baseRef = "devel",
  headRef = "main",
  baseSha = "base-current",
  headSha = "head-current",
  baseRepository = "owner/repo",
  headRepository = "owner/repo",
  number = 396,
  metadata = {},
  files = [],
  runs = "success",
  editedTitle = false,
} = {}) {
  let now = 0;
  const failures = [];
  const infos = [];
  const baseVersion = baseRef === "main" ? "1.2.0" : "1.0.0";
  const headVersion = headRef === "devel" ? "1.3.0" : "1.2.0";
  const contents = {
    [`${baseSha}:custom_components/allpowers_ble/manifest.json`]: JSON.stringify({ version: baseVersion }),
    [`${headSha}:custom_components/allpowers_ble/manifest.json`]: JSON.stringify({
      version: headVersion,
    }),
    [`${baseSha}:.release-please-manifest.json`]: JSON.stringify({ ".": baseVersion }),
    [`${headSha}:.release-please-manifest.json`]: JSON.stringify({ ".": headVersion }),
    [`${baseSha}:CHANGELOG.md`]: `# Changelog\n\n${heading(baseVersion)}\n`,
    [`${headSha}:CHANGELOG.md`]: `# Changelog\n\n${heading(headVersion)}\n\n${heading(baseVersion)}\n`,
    ...metadata,
  };
  const pullRequest = {
    number,
    title: "chore(ci): synchronize main into devel",
    draft: false,
    user: { login: "maintainer", type: "User" },
    base: { ref: baseRef, sha: baseSha, repo: { full_name: baseRepository } },
    head: { ref: headRef, sha: headSha, repo: { full_name: headRepository } },
  };
  const association = (associationBase = baseSha, associationHead = headSha, prNumber = number) => ({
    number: prNumber,
    base: { sha: associationBase },
    head: { sha: associationHead },
  });
  const successRuns = CHECK_NAMES.map((name, index) => ({
    id: index + 10,
    name,
    status: "completed",
    conclusion: "success",
    pull_requests: [association()],
  }));
  let checkRuns = successRuns;
  if (runs === "old-base") {
    checkRuns = successRuns.map((run) => ({
      ...run,
      pull_requests: [association("previous-base")],
    }));
  } else if (runs === "other-pr") {
    checkRuns = successRuns.map((run) => ({
      ...run,
      pull_requests: [association(baseSha, headSha, number + 1)],
    }));
  } else if (runs === "pending-current") {
    checkRuns = successRuns.flatMap((run) => [
      { ...run, id: run.id - 10 },
      run.name === CHECK_NAMES[0]
        ? { ...run, id: run.id + 100, status: "in_progress", conclusion: null }
        : run,
    ]);
  } else if (runs === "failed-current") {
    checkRuns = successRuns.flatMap((run) => [
      { ...run, id: run.id - 10 },
      run.name === CHECK_NAMES[0]
        ? { ...run, id: run.id + 100, conclusion: "failure" }
        : run,
    ]);
  } else if (runs === "edited-title") {
    checkRuns = successRuns.filter((run) => run.name !== "Validate title");
  }
  const workflowRuns = new Map();
  checkRuns = checkRuns.map((run) => {
    const workflowRunId = run.id + 1000;
    workflowRuns.set(workflowRunId, {
      event: runs === "push-current" ? "push" : "pull_request",
      head_sha: headSha,
      pull_requests: run.pull_requests,
    });
    return {
      ...run,
      details_url: `https://github.com/owner/repo/actions/runs/${workflowRunId}/job/1`,
    };
  });

  const github = {
    paginate: async () => files,
    rest: {
      pulls: {
        get: async () => ({
          data: {
            title: pullRequest.title,
            base: { ref: baseRef, sha: baseSha },
            head: { sha: headSha },
          },
        }),
      },
      checks: {
        listForRef: async () => ({ data: { check_runs: checkRuns } }),
      },
      actions: {
        getWorkflowRun: async ({ run_id: runId }) => ({
          data: workflowRuns.get(runId),
        }),
      },
      repos: {
        getContent: async ({ path: filePath, ref }) => {
          const content = contents[`${ref}:${filePath}`];
          if (content === undefined) throw new Error(`Missing test fixture: ${ref}:${filePath}`);
          return {
            data: {
              type: "file",
              content: Buffer.from(content, "utf8").toString("base64"),
            },
          };
        },
        compareCommits: async () => ({ data: { status: "ahead" } }),
      },
    },
  };
  const context = {
    repo: { owner: "owner", repo: "repo" },
    payload: {
      action: editedTitle ? "edited" : "opened",
      changes: editedTitle ? { title: { from: "old", to: pullRequest.title } } : {},
      pull_request: pullRequest,
    },
  };
  const core = {
    setFailed: (message) => failures.push(message),
    info: (message) => infos.push(message),
  };
  const process = { env: { REQUIRE_HIL_STABLE_GATE: "false" } };
  const fakeDate = { now: () => now };
  const fakeSetTimeout = (callback, duration) => {
    now += duration;
    callback();
    return 0;
  };
  return {
    contents,
    pullRequest,
    context,
    github,
    core,
    process,
    fakeDate,
    fakeSetTimeout,
    failures,
    infos,
    async run() {
      await runWorkflowScript(
        github,
        context,
        core,
        process,
        Buffer,
        fakeDate,
        fakeSetTimeout,
      );
    },
  };
}

function setContent(state, ref, name, value) {
  state.contents[`${ref}:${name}`] = value;
}

test("authorized same-repository main-to-devel sync accepts newer release metadata", async () => {
  const state = fixture({ files: [
    { filename: "CHANGELOG.md" },
    { filename: ".release-please-manifest.json" },
    { filename: "custom_components/allpowers_ble/manifest.json" },
  ] });
  await state.run();
  assert.deepEqual(state.failures, []);
  assert.ok(state.infos.includes("All required checks completed successfully."));
});

test("release metadata rejects version rollback, lost manifest history, and malformed versions", async (t) => {
  await t.test("integration version rollback", async () => {
    const state = fixture({ files: [{ filename: "CHANGELOG.md" }] });
    setContent(state, "head-current", "custom_components/allpowers_ble/manifest.json", JSON.stringify({ version: "0.9.0" }));
    await state.run();
    assert.match(state.failures[0], /version regressed/);
  });
  await t.test("release-please package history removal", async () => {
    const state = fixture({ files: [{ filename: "CHANGELOG.md" }] });
    setContent(state, "base-current", ".release-please-manifest.json", JSON.stringify({ ".": "1.0.0", old_package: "0.9.0" }));
    await state.run();
    assert.match(state.failures[0], /history was removed for old_package/);
  });
  await t.test("malformed version", async () => {
    const state = fixture({ files: [{ filename: "CHANGELOG.md" }] });
    setContent(state, "head-current", ".release-please-manifest.json", JSON.stringify({ ".": "not-a-version" }));
    await state.run();
    assert.match(state.failures[0], /Invalid SemVer/);
  });
});

test("metadata gate rejects a missing historical changelog heading", async () => {
  const state = fixture({ files: [{ filename: "CHANGELOG.md" }] });
  setContent(state, "base-current", "CHANGELOG.md", `# Changelog\n\n${heading("1.0.0")}\n\n${heading("0.9.0")}\n`);
  setContent(state, "head-current", "CHANGELOG.md", `# Changelog\n\n${heading("1.2.0")}\n\n${heading("1.0.0")}\n`);
  await state.run();
  assert.match(state.failures[0], /Changelog history was removed for 0\.9\.0/);
});

test("promotion still rejects unauthorized version changes", async () => {
  const state = fixture({ baseRef: "main", headRef: "devel", files: [
    { filename: "custom_components/allpowers_ble/manifest.json" },
  ] });
  await state.run();
  assert.match(state.failures[0], /Promotion must preserve the integration version/);
});

test("promotion with unchanged release metadata still passes", async () => {
  const state = fixture({ baseRef: "main", headRef: "devel", files: [{ filename: "README.md" }] });
  setContent(state, "head-current", "custom_components/allpowers_ble/manifest.json", JSON.stringify({ version: "1.2.0" }));
  setContent(state, "head-current", ".release-please-manifest.json", JSON.stringify({ ".": "1.2.0" }));
  setContent(state, "head-current", "CHANGELOG.md", state.contents["base-current:CHANGELOG.md"]);
  await state.run();
  assert.deepEqual(state.failures, []);
  assert.ok(state.infos.includes("All required checks completed successfully."));
});

test("a fork named main cannot use the changelog exception; feature PR behavior remains normal", async (t) => {
  await t.test("fork main to devel", async () => {
    const state = fixture({ headRepository: "fork/repo", files: [{ filename: "CHANGELOG.md" }] });
    await state.run();
    assert.match(state.failures[0], /CHANGELOG.md is managed/);
  });
  await t.test("missing repository identity fails closed", async () => {
    const state = fixture({ baseRepository: null, files: [{ filename: "CHANGELOG.md" }] });
    await state.run();
    assert.match(state.failures[0], /CHANGELOG.md is managed/);
  });
  await t.test("ordinary feature PR", async () => {
    const state = fixture({ headRef: "feature/docs", files: [{ filename: "README.md" }] });
    await state.run();
    assert.deepEqual(state.failures, []);
    assert.ok(state.infos.includes("All required checks completed successfully."));
  });
});

test("required checks must identify the current PR and exact base/head", async (t) => {
  await t.test("green checks from an older base do not satisfy the gate", async () => {
    const state = fixture({ runs: "old-base" });
    await state.run();
    assert.match(state.failures[0], /Missing required checks/);
  });
  await t.test("green checks associated with another PR do not satisfy the gate", async () => {
    const state = fixture({ runs: "other-pr" });
    await state.run();
    assert.match(state.failures[0], /Missing required checks/);
  });
  await t.test("green checks from a main push do not satisfy the gate", async () => {
    const state = fixture({ runs: "push-current" });
    await state.run();
    assert.match(state.failures[0], /Missing required checks/);
  });
  await t.test("positive current PR/base/head associations pass", async () => {
    const state = fixture();
    await state.run();
    assert.deepEqual(state.failures, []);
    assert.ok(state.infos.includes("All required checks completed successfully."));
  });
});

test("a newer pending or failing current check cannot be hidden by an older success", async (t) => {
  await t.test("pending current check times out", async () => {
    const state = fixture({ runs: "pending-current" });
    await state.run();
    assert.match(state.failures[0], /Timeout waiting for required checks/);
  });
  await t.test("failing current check fails", async () => {
    const state = fixture({ runs: "failed-current" });
    await state.run();
    assert.match(state.failures[0], /Required checks failed/);
  });
});

test("title edit keeps the inline title-check exception", async () => {
  const state = fixture({ runs: "edited-title", editedTitle: true });
  await state.run();
  assert.deepEqual(state.failures, []);
  assert.ok(state.infos.includes("All required checks completed successfully."));
});
