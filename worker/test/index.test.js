import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  normalizeGasPrice,
  normalizeStockMovers,
  parseFeed,
  selectLatest,
} from "../src/index.js";

const now = Date.parse("2026-11-18T12:00:00Z");

test("parses recent RSS headlines and decodes feed text", () => {
  const xml = `<?xml version="1.0"?>
    <rss><channel><item>
      <title><![CDATA[Breaking: Storm &amp; flooding &amp;#8212; alert]]></title>
      <link>https://example.com/storm</link>
      <pubDate>Wed, 18 Nov 2026 11:45:00 GMT</pubDate>
    </item></channel></rss>`;

  assert.deepEqual(parseFeed(xml, "Example News", now), [{
    title: "Breaking: Storm & flooding — alert",
    link: "https://example.com/storm",
    outlet: "Example News",
    published_at: "2026-11-18T11:45:00.000Z",
  }]);
});

test("parses Atom links and rejects stale or unsafe entries", () => {
  const xml = `<feed xmlns="http://www.w3.org/2005/Atom">
    <entry>
      <title>New talks announced</title>
      <link rel="alternate" href="https://example.com/talks" />
      <updated>2026-11-18T11:30:00Z</updated>
    </entry>
    <entry>
      <title>Old story</title>
      <link href="https://example.com/old" />
      <updated>2026-11-10T11:30:00Z</updated>
    </entry>
    <entry>
      <title>Unsafe link</title>
      <link href="javascript:alert(1)" />
      <updated>2026-11-18T11:30:00Z</updated>
    </entry>
  </feed>`;

  assert.deepEqual(parseFeed(xml, "Example News", now).map((item) => item.title), [
    "New talks announced",
  ]);
});

test("sorts latest items and removes duplicate headlines", () => {
  const items = [
    { title: "Same story", published_at: "2026-11-18T11:00:00Z" },
    { title: "Another story", published_at: "2026-11-18T11:30:00Z" },
    { title: "Same-story!", published_at: "2026-11-18T11:45:00Z" },
  ];

  assert.deepEqual(selectLatest(items).map((item) => item.title), [
    "Same-story!",
    "Another story",
  ]);
});

test("normalizes the EIA Lower Atlantic weekly regular-gas price", () => {
  const snapshot = normalizeGasPrice({
    response: {
      data: [{
        duoarea: "R1Z",
        product: "EPMRU",
        process: "PTE",
        period: "2026-11-16",
        value: "3.406",
        units: "$/GAL",
      }],
    },
  }, now);

  assert.deepEqual(snapshot, {
    updated_at: "2026-11-18T12:00:00.000Z",
    period: "2026-11-16",
    price: 3.406,
  });
  assert.throws(() => normalizeGasPrice({
    response: { data: [{
      duoarea: "SGA",
      product: "EPMRU",
      process: "PTE",
      period: "2026-11-16",
      value: "3.406",
      units: "$/GAL",
    }] },
  }, now), /no usable Lower Atlantic gasoline price/);
});

test("gas endpoint reports when its EIA key is not configured", async () => {
  const response = await worker.fetch(
    new Request("https://worker.example/gas.json"),
    { ALLOWED_ORIGIN: "https://dashboard.example", LATEST_NEWS: { get: async () => null } }
  );

  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "EIA gas data is not configured." });
});

test("normalizes the top three Yahoo Finance daily gainers", () => {
  const marketTime = Math.floor(now / 1000);
  const snapshot = normalizeStockMovers({
    finance: {
      result: [{
        quotes: [
          {
            symbol: "BAD",
            shortName: "Incomplete quote",
            regularMarketPrice: null,
            regularMarketChangePercent: 99,
          },
          {
            symbol: "NVDA",
            shortName: "NVIDIA Corporation",
            regularMarketPrice: 165.05,
            regularMarketChangePercent: 31.4825,
            regularMarketTime: marketTime,
          },
          {
            symbol: "KYMR",
            shortName: "Kymera Therapeutics, Inc.",
            regularMarketPrice: 150.37,
            regularMarketChangePercent: 30.405,
            regularMarketTime: marketTime,
          },
          {
            symbol: "AXTI",
            shortName: "AXT Inc",
            regularMarketPrice: 38.02,
            regularMarketChangePercent: 24.3703,
            regularMarketTime: marketTime,
          },
          {
            symbol: "IREN",
            shortName: "IREN Limited",
            regularMarketPrice: 14.64,
            regularMarketChangePercent: 23.8579,
            regularMarketTime: marketTime,
          },
        ],
      }],
    },
  }, now);

  assert.deepEqual(snapshot, {
    updated_at: "2026-11-18T12:00:00.000Z",
    as_of: "2026-11-18T12:00:00.000Z",
    items: [
      {
        symbol: "NVDA",
        name: "NVIDIA Corporation",
        price: 165.05,
        change_percent: 31.4825,
      },
      {
        symbol: "KYMR",
        name: "Kymera Therapeutics, Inc.",
        price: 150.37,
        change_percent: 30.405,
      },
      {
        symbol: "AXTI",
        name: "AXT Inc",
        price: 38.02,
        change_percent: 24.3703,
      },
    ],
  });
});

test("stocks endpoint returns the cached daily movers", async () => {
  const snapshot = {
    updated_at: "2026-11-18T12:00:00.000Z",
    as_of: "2026-11-18T12:00:00.000Z",
    items: [{ symbol: "NVDA", name: "NVIDIA Corporation", price: 165.05, change_percent: 31.4825 }],
  };
  const response = await worker.fetch(
    new Request("https://worker.example/stocks.json"),
    {
      ALLOWED_ORIGIN: "https://dashboard.example",
      LATEST_NEWS: {
        get: async (key) => key === "stock-movers" ? snapshot : null,
      },
    }
  );

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), snapshot);
});
