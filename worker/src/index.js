import { XMLParser } from "fast-xml-parser";

const MAX_AGE_MS = 72 * 60 * 60 * 1000;
const MAX_ARTICLES_PER_FEED = 20;
const MAX_TICKER_ITEMS = 30;
const REQUEST_TIMEOUT_MS = 12_000;
const SNAPSHOT_KEY = "latest";

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
    if (request.method !== "GET" || url.pathname !== "/latest.json") {
      return jsonResponse({ error: "Not found." }, 404, env.ALLOWED_ORIGIN);
    }
    const snapshot = await env.LATEST_NEWS.get(SNAPSHOT_KEY, { type: "json" });
    if (!snapshot) {
      return jsonResponse({ error: "The first live snapshot is not ready yet." }, 503, env.ALLOWED_ORIGIN);
    }
    return jsonResponse(snapshot, 200, env.ALLOWED_ORIGIN);
  },

  async scheduled(_controller, env, context) {
    context.waitUntil(
      updateSnapshot(env).catch((error) => {
        console.error("Live news refresh failed:", error);
      })
    );
  },
};
