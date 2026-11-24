import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  normalizeGasPrice,
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
