import { XMLParser } from "fast-xml-parser";

const MAX_AGE_MS = 72 * 60 * 60 * 1000;
const MAX_ARTICLES_PER_FEED = 20;
const MAX_TICKER_ITEMS = 30;
const REQUEST_TIMEOUT_MS = 12_000;
const STOCK_MOVERS_REFRESH_MS = 10 * 60 * 1000;
const SNAPSHOT_KEY = "latest";
const GAS_PRICE_KEY = "gas-price";
const STOCK_MOVERS_KEY = "stock-movers";

const parser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: "@_",
  removeNSPrefix: true,
  cdataPropName: "__cdata",
  parseTagValue: false,
  trimValues: true,
});

function asArray(value) {
  if (value === undefined || value === null) return [];
  return Array.isArray(value) ? value : [value];
}

function textValue(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return textValue(value[0]);
  if (value && typeof value === "object") {
    if (typeof value["#text"] === "string") return value["#text"];
    if (typeof value.__cdata === "string") return value.__cdata;
  }
  return "";
}

function plainText(value) {
  return textValue(value)
    .replace(/<[^>]*>/g, " ")
    .replace(/&nbsp;|&#160;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&#(x[0-9a-f]+|\d+);/gi, (_, code) => {
      const point = code[0].toLowerCase() === "x"
        ? Number.parseInt(code.slice(1), 16)
        : Number.parseInt(code, 10);
      return Number.isFinite(point) && point > 0 && point <= 0x10ffff
        && (point < 0xd800 || point > 0xdfff)
        ? String.fromCodePoint(point)
        : " ";
    })
    .replace(/\s+/g, " ")
    .trim();
}

function itemLink(value) {
  const links = asArray(value);
  const alternate = links.find((link) =>
    link && typeof link === "object" && (!link["@_rel"] || link["@_rel"] === "alternate")
  ) || links[0];
  const raw = typeof alternate === "string"
    ? alternate
    : alternate && typeof alternate === "object"
      ? alternate["@_href"] || textValue(alternate)
      : "";
  try {
    const url = new URL(raw);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
  } catch {
    return "";
  }
}

function entryList(document) {
  return asArray(
    document.rss?.channel?.item
      ?? document.feed?.entry
      ?? document.RDF?.item
  );
}

export function parseFeed(xml, outlet, now = Date.now()) {
  const document = parser.parse(xml);
  return entryList(document).slice(0, MAX_ARTICLES_PER_FEED).flatMap((entry) => {
    const title = plainText(entry.title);
    const link = itemLink(entry.link);
    const parsedDate = Date.parse(
      plainText(entry.pubDate || entry.published || entry.updated || entry.date)
    );
    const published = Number.isFinite(parsedDate) ? parsedDate : now;
    if (!title || !link || !Number.isFinite(published)) return [];
    if (published < now - MAX_AGE_MS || published > now + 5 * 60 * 1000) return [];
    return [{
      title,
      link,
      outlet,
      published_at: new Date(published).toISOString(),
    }];
  });
}

function titleKey(title) {
  return title.normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

export function selectLatest(items) {
  const seen = new Set();
  return [...items]
    .sort((left, right) => Date.parse(right.published_at) - Date.parse(left.published_at))
    .filter((item) => {
      const key = titleKey(item.title);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, MAX_TICKER_ITEMS);
}

export function normalizeGasPrice(payload, now = Date.now()) {
  const row = payload?.response?.data?.[0];
  const price = Number(row?.value);
  if (
    !row
    || row.duoarea !== "R1Z"
    || row.product !== "EPMRU"
    || row.process !== "PTE"
    || !/^\d{4}-\d{2}-\d{2}$/.test(row.period)
    || !Number.isFinite(price)
    || price <= 0
    || row.units !== "$/GAL"
  ) {
    throw new Error("EIA response contained no usable Lower Atlantic gasoline price.");
  }
  return {
    updated_at: new Date(now).toISOString(),
    period: row.period,
    price,
  };
}

export function normalizeStockMovers(payload, now = Date.now()) {
  const quotes = payload?.finance?.result?.[0]?.quotes;
  if (!Array.isArray(quotes)) {
    throw new Error("Yahoo Finance response contained no stock movers.");
  }

  const sourceTimes = [];
  const items = quotes.flatMap((quote) => {
    const symbol = typeof quote?.symbol === "string" ? quote.symbol.trim() : "";
    const name = (typeof quote?.shortName === "string" && quote.shortName.trim())
      || (typeof quote?.longName === "string" && quote.longName.trim())
      || "";
    if (
      quote?.regularMarketPrice === null
      || quote?.regularMarketPrice === undefined
      || quote?.regularMarketChangePercent === null
      || quote?.regularMarketChangePercent === undefined
    ) return [];
    const price = Number(quote?.regularMarketPrice);
    const changePercent = Number(quote?.regularMarketChangePercent);
    const sourceTime = Number(quote?.regularMarketTime);
    if (
      !/^[A-Z0-9.-]{1,12}$/.test(symbol)
      || !name
      || !Number.isFinite(price)
      || price <= 0
      || !Number.isFinite(changePercent)
    ) return [];
    if (Number.isFinite(sourceTime) && sourceTime > 0) sourceTimes.push(sourceTime);
    return [{ symbol, name, price, change_percent: changePercent }];
  }).slice(0, 3);

  if (items.length === 0) {
    throw new Error("Yahoo Finance response contained no usable stock movers.");
  }
  const asOf = sourceTimes.length > 0
    ? Math.max(...sourceTimes) * 1000
    : now;
  return {
    updated_at: new Date(now).toISOString(),
    as_of: new Date(asOf).toISOString(),
    items,
  };
}

async function fetchFeed(feed) {
  const url = new URL(feed.url);
  if (url.protocol !== "https:") throw new Error("Feed URL must use HTTPS.");
  const response = await fetch(url, {
    headers: {
      "User-Agent": "MorningNews/1.0 (+https://github.com/naokib23/MorningNews)",
      Accept: "application/rss+xml, application/atom+xml, application/xml, text/xml",
    },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error("HTTP " + response.status);
  return parseFeed(await response.text(), feed.name);
}

async function updateSnapshot(env) {
  const configResponse = await fetch(env.FEEDS_URL, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!configResponse.ok) throw new Error("Unable to load public feed configuration.");
  const feeds = await configResponse.json();
  if (!Array.isArray(feeds) || feeds.length === 0) {
    throw new Error("Public feed configuration is empty or invalid.");
  }

  const results = await Promise.allSettled(feeds.map(fetchFeed));
  const items = [];
  for (const result of results) {
    if (result.status === "fulfilled") {
      items.push(...result.value);
    } else {
      console.warn("A live news feed failed:", result.reason);
    }
  }
  const latest = selectLatest(items);
  if (latest.length === 0) throw new Error("No recent items were received from any feed.");

  const snapshot = {
    updated_at: new Date().toISOString(),
    items: latest,
  };
  await env.LATEST_NEWS.put(SNAPSHOT_KEY, JSON.stringify(snapshot), {
    expirationTtl: 24 * 60 * 60,
  });
  console.log("Published", latest.length, "latest headlines.");
}

async function updateGasPrice(env) {
  if (!env.EIA_API_KEY) return;
  const current = await env.LATEST_NEWS.get(GAS_PRICE_KEY, { type: "json" });
  if (
    current
    && Number.isFinite(Date.parse(current.updated_at))
    && Date.now() - Date.parse(current.updated_at) < 24 * 60 * 60 * 1000
  ) return;

  const url = new URL("https://api.eia.gov/v2/petroleum/pri/gnd/data/");
  url.searchParams.set("api_key", env.EIA_API_KEY);
  url.searchParams.set("frequency", "weekly");
  url.searchParams.set("data[]", "value");
  url.searchParams.set("facets[duoarea][]", "R1Z");
  url.searchParams.set("facets[product][]", "EPMRU");
  url.searchParams.set("facets[process][]", "PTE");
  url.searchParams.set("sort[0][column]", "period");
  url.searchParams.set("sort[0][direction]", "desc");
  url.searchParams.set("length", "1");
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error("EIA returned HTTP " + response.status);
  const snapshot = normalizeGasPrice(await response.json());
  await env.LATEST_NEWS.put(GAS_PRICE_KEY, JSON.stringify(snapshot), {
    expirationTtl: 14 * 24 * 60 * 60,
  });
  console.log("Published Lower Atlantic weekly gasoline price.");
}

async function updateStockMovers(env) {
  const current = await env.LATEST_NEWS.get(STOCK_MOVERS_KEY, { type: "json" });
  if (
    current
    && Number.isFinite(Date.parse(current.updated_at))
    && Date.now() - Date.parse(current.updated_at) < STOCK_MOVERS_REFRESH_MS
  ) return;

  const url = new URL("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved");
  url.searchParams.set("count", "10");
  url.searchParams.set("scrIds", "day_gainers");
  const response = await fetch(url, {
    headers: {
      Accept: "application/json",
      "User-Agent": "Mozilla/5.0 (compatible; MorningNews/1.0; +https://github.com/naokib23/MorningNews)",
    },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error("Yahoo Finance returned HTTP " + response.status);
  const snapshot = normalizeStockMovers(await response.json());
  await env.LATEST_NEWS.put(STOCK_MOVERS_KEY, JSON.stringify(snapshot), {
    expirationTtl: 24 * 60 * 60,
  });
  console.log("Published", snapshot.items.length, "daily stock movers.");
}

function jsonResponse(body, status, origin) {
  return Response.json(body, {
    status,
    headers: {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Cache-Control": "no-store",
      "Vary": "Origin",
    },
  });
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin");
    if (origin && origin !== env.ALLOWED_ORIGIN) {
      return new Response("Origin not allowed.", { status: 403 });
    }
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN,
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
          "Vary": "Origin",
        },
      });
    }
    const url = new URL(request.url);
    if (
      request.method !== "GET"
      || !["/latest.json", "/gas.json", "/stocks.json"].includes(url.pathname)
    ) {
      return jsonResponse({ error: "Not found." }, 404, env.ALLOWED_ORIGIN);
    }
    const isGasPrice = url.pathname === "/gas.json";
    const isStockMovers = url.pathname === "/stocks.json";
    if (isGasPrice && !env.EIA_API_KEY) {
      return jsonResponse({ error: "EIA gas data is not configured." }, 503, env.ALLOWED_ORIGIN);
    }
    const snapshot = await env.LATEST_NEWS.get(
      isGasPrice
        ? GAS_PRICE_KEY
        : isStockMovers
          ? STOCK_MOVERS_KEY
          : SNAPSHOT_KEY,
      { type: "json" }
    );
    if (!snapshot) {
      return jsonResponse({
        error: isGasPrice
          ? "The first EIA gas snapshot is not ready yet."
          : isStockMovers
            ? "The first stock-movers snapshot is not ready yet."
            : "The first live snapshot is not ready yet.",
      }, 503, env.ALLOWED_ORIGIN);
    }
    return jsonResponse(snapshot, 200, env.ALLOWED_ORIGIN);
  },

  async scheduled(_controller, env, context) {
    context.waitUntil(Promise.all([
      updateSnapshot(env).catch((error) => {
        console.error("Live news refresh failed:", error);
      }),
      updateGasPrice(env).catch((error) => {
        console.error("Gas price refresh failed:", error);
      }),
      updateStockMovers(env).catch((error) => {
        console.error("Stock movers refresh failed:", error);
      }),
    ]));
  },
};
