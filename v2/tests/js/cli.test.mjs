import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../../..");
const fakeFetch = pathToFileURL(path.join(here, "fake-fetch.mjs")).href;
const collector = path.join(root, "pionex-grid-api.mjs");

test("the collector CLI writes Records from the fake exchange", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pionex-cli-"));
  const creds = path.join(dir, "creds.txt");
  const output = path.join(dir, "out.json");
  fs.writeFileSync(creds, "test-key\ntest-secret\n", "utf8");
  try {
    const probe = spawnSync(process.execPath, [
      "--import", fakeFetch,
      "--eval",
      "fetch('http://127.0.0.1:9/probe').then(() => process.exit(2), (error) => { if (!String(error).includes('blocked non-fixture host')) process.exit(3); });",
    ], { encoding: "utf8", timeout: 10000 });
    assert.equal(probe.status, 0, probe.stderr || probe.stdout);

    const run = spawnSync(process.execPath, [
      "--import", fakeFetch,
      collector,
      "--credentials", creds,
      "--include-finished",
      "--output", output,
    ], { encoding: "utf8", timeout: 15000, cwd: root });
    assert.equal(run.status, 0, run.stderr || run.stdout);
    const snapshot = JSON.parse(fs.readFileSync(output, "utf8"));
    const printed = JSON.parse(run.stdout);
    assert.equal(printed.Records.length, snapshot.Records.length);
    assert.equal(snapshot.ok, true);
    assert.equal(snapshot.IncludeFinished, true);
    assert.equal(snapshot.ContractCount, 1);
    assert.equal(snapshot.FinishedCount, 1);
    assert.equal(snapshot.FinishedFetched, 1);
    assert.equal(snapshot.FinishedReused, 0);
    assert.equal(snapshot.RunningSource, "detail");
    assert.equal(snapshot.Wallet.totalInUsdt, "100");

    const byId = Object.fromEntries(snapshot.Records.map((record) => [record.ApiOrderId, record]));
    const usdt = byId["bot-usdt"];
    assert.equal(usdt.Symbol, "BTC/USDT");
    assert.equal(usdt.ListStatus, "running");
    assert.equal(usdt.Product, "contract_grid");
    assert.equal(usdt.Investment, 1000);
    assert.equal(usdt.GridProfit, 12.5);
    assert.equal(usdt.MarkPrice, 110000);
    assert.equal(usdt.Leverage, "5x long");
    assert.equal(usdt.ProfitCurrency, "USDT");
    assert.equal(usdt.Complete, true);
    assert.equal(usdt.Key, `${usdt.Symbol}|${usdt.Created}`);
    assert.equal(Object.hasOwn(usdt, "ConversionStale"), false);

    const coin = byId["bot-eth"];
    assert.equal(coin.Symbol, "ETH");
    assert.equal(coin.ListStatus, "finished");
    assert.equal(coin.Product, "coin_margined_contract_grid");
    assert.equal(coin.Investment, 20);
    assert.equal(coin.GridProfit, 20);
    assert.equal(coin.RawGridProfit, 2);
    assert.equal(coin.ConversionPrice, 10);
    assert.equal(coin.ConversionSymbol, "ETH_USDT");
    assert.equal(coin.MarkPrice, 2400);
    assert.equal(coin.Leverage, "3x long");
    assert.equal(coin.Complete, true);
    assert.equal(coin.Key, `${coin.Symbol}|${coin.Created}`);
    assert.equal(Object.hasOwn(coin, "ConversionStale"), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
