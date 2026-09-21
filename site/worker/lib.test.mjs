// node --test site/worker/lib.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { Invalid, aggregate, escapeHtml, isOutlier, median, renderTable, validatePayload, validHardware }
  from "./lib.js";

const row = (over = {}) => ({
  model: "qwen2.5-coder:14b", quant: "Q4_K_M", params: "14.8B", num_ctx: 8192, hw: "gpu12",
  category: "tests", score: 1, gen_tps: 49.4, gpu_ratio: 1, runs: 2, ...over,
});
const payload = (over = {}) => ({ suite: "2026.09", routeai: "0.3.0", os: "windows", bench_date: "2026-09-21",
  rows: [row()], ...over });

test("a well formed submission keeps only the known fields", () => {
  const clean = validatePayload({ ...payload(), rows: [{ ...row(), node: "desktop-gpu", url: "http://10.0.0.5:11434",
    instruction: "write tests for src/secret.py" }] });
  assert.deepEqual(Object.keys(clean.rows[0]).sort(),
    ["category", "gen_tps", "gpu_ratio", "hw", "model", "num_ctx", "params", "quant", "runs", "score"]);
  assert.equal(JSON.stringify(clean).includes("desktop-gpu"), false);
  assert.equal(JSON.stringify(clean).includes("10.0.0.5"), false);
  assert.equal(JSON.stringify(clean).includes("secret"), false);
});

test("submissions that are not plausible are refused", () => {
  const bad = [
    [{ suite: "2025.01" }, /unknown benchmark suite/],
    [{ routeai: "nightly" }, /version/],
    [{ rows: [] }, /non-empty/],
    [{ rows: [row({ score: 1.5 })] }, /score/],
    [{ rows: [row({ gen_tps: 0 })] }, /gen_tps/],
    [{ rows: [row({ category: "everything" })] }, /category/],
    [{ rows: [row({ hw: "gpu13" })] }, /hw/],
    [{ rows: [row({ hw: "<script>" })] }, /hw/],
    [{ rows: [row({ model: "qwen<script>" })] }, /model/],
    [{ rows: [row({ num_ctx: 10 })] }, /num_ctx/],
    [{ rows: [row(), row()] }, /duplicate/],
    [{ os: "Windows 11 on Federico's PC" }, /os/],
  ];
  for (const [over, message] of bad) {
    assert.throws(() => validatePayload({ ...payload(), ...over }), (err) => err instanceof Invalid && message.test(err.message),
      JSON.stringify(over));
  }
  assert.throws(() => validatePayload(null), Invalid);
});

test("hardware buckets", () => {
  assert.equal(validHardware("cpu"), "cpu");
  assert.equal(validHardware("gpu24"), "gpu24");
  assert.throws(() => validHardware("gpu7"), Invalid);
  assert.throws(() => validHardware("gpu"), Invalid);
});

test("median and outliers need several independent submitters", () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 2, 3]), 2.5);
  const peers = [48, 49, 50, 51, 52];
  assert.equal(isOutlier(49, peers), false);
  assert.equal(isOutlier(500, peers), true);   // someone claiming ten times the speed
  assert.equal(isOutlier(5, peers), true);
  assert.equal(isOutlier(500, [48, 49]), false); // too few peers to judge
});

test("aggregates keep certified and uncertified apart and count people, not rows", () => {
  const rows = [
    { certified: 1, subject: "gh:a", model: "m", quant: "Q4", params: "7B", hw: "cpu", category: "docs", score: 1, gen_tps: 8 },
    { certified: 1, subject: "gh:a", model: "m", quant: "Q4", params: "7B", hw: "cpu", category: "docs", score: 0.8, gen_tps: 10 },
    { certified: 1, subject: "gh:b", model: "m", quant: "Q4", params: "7B", hw: "cpu", category: "docs", score: 0.6, gen_tps: 12 },
    { certified: 0, subject: "anon:c", model: "m", quant: "Q4", params: "7B", hw: "cpu", category: "docs", score: 0.2, gen_tps: 99 },
  ];
  const groups = aggregate(rows);
  assert.equal(groups.length, 2);
  const certified = groups.find((g) => g.certified);
  assert.equal(certified.users, 2);
  assert.equal(certified.samples, 3);
  assert.equal(certified.score, 0.8);
  assert.equal(certified.gen_tps, 10);
  assert.equal(groups.find((g) => !g.certified).users, 1);
});

test("the rendered table escapes everything that came from a submission", () => {
  const labels = { model: "Model", quant: "Q", hardware: "HW", category: "Cat", score: "S", speed: "T/s",
    users: "People", empty: "Nothing yet" };
  const html = renderTable([{ certified: true, model: 'x"><img src=x onerror=alert(1)>', quant: "Q4", params: "",
    hw: "gpu12", category: "code", score: 0.5, gen_tps: 20, users: 1, samples: 1 }], labels);
  assert.equal(html.includes("<img"), false);
  assert.match(html, /&lt;img/);
  assert.match(html, /GPU 12 GB/);
  assert.equal(renderTable([], labels), '<p class="note">Nothing yet</p>');
  assert.equal(escapeHtml("<a>&'\""), "&lt;a&gt;&amp;&#39;&quot;");
});
