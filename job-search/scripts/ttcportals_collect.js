#!/usr/bin/env node

const fs = require("fs");
const { chromium } = require("playwright");

function normalizeText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function truthy(value, fallback) {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "boolean") return value;
  return !["0", "false", "no", "off"].includes(String(value).trim().toLowerCase());
}

function readSource() {
  const raw = fs.readFileSync(0, "utf8").trim();
  if (!raw) throw new Error("Expected a source configuration on stdin.");
  const source = JSON.parse(raw);
  if (!source || typeof source !== "object" || Array.isArray(source)) {
    throw new Error("Source configuration must be a JSON object.");
  }
  return source;
}

function listingUrls(source) {
  const configured = Array.isArray(source.listing_urls)
    ? source.listing_urls
    : [source.url];
  const urls = configured.map((value) => String(value || "").trim()).filter(Boolean);
  if (!urls.length) throw new Error("No TalentTech Portals listing URL was configured.");
  const allowedCustomHosts = new Set(
    (Array.isArray(source.allowed_custom_hosts) ? source.allowed_custom_hosts : [])
      .map((value) => String(value || "").trim().toLowerCase())
      .filter(Boolean),
  );
  for (const value of urls) {
    const parsed = new URL(value);
    if (
      !parsed.hostname.endsWith(".ttcportals.com") &&
      !allowedCustomHosts.has(parsed.hostname.toLowerCase())
    ) {
      throw new Error(`Unsupported TalentTech Portals host: ${parsed.hostname}`);
    }
  }
  return urls;
}

function pageUrl(baseUrl, pageNumber) {
  if (pageNumber <= 1) return baseUrl;
  const parsed = new URL(baseUrl);
  parsed.searchParams.set("page", String(pageNumber));
  return parsed.toString();
}

async function loadListing(page, url, timeoutMs, attempts, waitMs) {
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
      await page.waitForTimeout(waitMs);
      const title = await page.title();
      const bodyText = normalizeText(await page.locator("body").innerText());
      const challenged =
        /just a moment|attention required/i.test(title) ||
        /performing security verification|verify you are human/i.test(bodyText);
      if (challenged) throw new Error("Cloudflare security verification blocked the listing page.");
      return;
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await page.waitForTimeout(waitMs * attempt);
    }
  }
  throw lastError || new Error(`Could not load ${url}`);
}

async function extractJobs(page, sourceUrl) {
  return page.locator(".jobs-section__item").evaluateAll(
    (items, listingUrl) =>
      items
        .map((item) => {
          const anchor = Array.from(item.querySelectorAll("a[href]")).find((candidate) => {
            try {
              return /^\/jobs\/\d+/.test(new URL(candidate.href).pathname);
            } catch {
              return false;
            }
          });
          if (!anchor) return null;
          const parsed = new URL(anchor.href);
          const idMatch = parsed.pathname.match(/^\/jobs\/(\d+)/);
          const locationNode = item.querySelector(".large-4.columns, [class*='job-location']");
          const location = (locationNode?.innerText || "")
            .replace(/^\s*Location:\s*/i, "")
            .replace(/\u00a0/g, " ")
            .replace(/\s+/g, " ")
            .trim();
          const isNew = Array.from(item.querySelectorAll("sup, .small-text")).some((node) =>
            /^new$/i.test((node.textContent || "").trim()),
          );
          return {
            role: (anchor.textContent || "").replace(/\s+/g, " ").trim(),
            url: anchor.href,
            location,
            external_job_id: idMatch ? idMatch[1] : "",
            is_new: isNew,
            source_url: listingUrl,
          };
        })
        .filter((job) => job && job.role && job.url),
    sourceUrl,
  );
}

async function main() {
  const source = readSource();
  const urls = listingUrls(source);
  const maxPages = Math.max(1, Math.min(Number(source.max_pages || 3), 20));
  const timeoutMs = Math.max(5000, Number(source.browser_timeout_ms || 25000));
  const attempts = Math.max(1, Math.min(Number(source.browser_retries || 2) + 1, 4));
  const waitMs = Math.max(250, Number(source.browser_wait_ms || 1200));
  const headless = truthy(source.browser_headless, true);
  const launchOptions = {
    channel: String(source.browser_channel || "chrome"),
    headless,
    args: headless ? [] : ["--window-position=-10000,-10000", "--window-size=100,100"],
  };
  const jobs = new Map();

  for (const [urlIndex, baseUrl] of urls.entries()) {
    const browser = await chromium.launch(launchOptions);
    try {
      const contextOptions = {};
      if (source.browser_user_agent) {
        contextOptions.userAgent = String(source.browser_user_agent);
      }
      const context = await browser.newContext(contextOptions);
      const page = await context.newPage();
      for (let pageNumber = 1; pageNumber <= maxPages; pageNumber += 1) {
        const currentUrl = pageUrl(baseUrl, pageNumber);
        try {
          await loadListing(page, currentUrl, timeoutMs, attempts, waitMs);
        } catch (error) {
          if (pageNumber === 1) throw error;
          process.stderr.write(
            `Stopped pagination for ${baseUrl} at page ${pageNumber}: ${error.message || error}\n`,
          );
          break;
        }
        const pageJobs = await extractJobs(page, currentUrl);
        let added = 0;
        for (const job of pageJobs) {
          const existing = jobs.get(job.url);
          if (!existing) {
            jobs.set(job.url, job);
            added += 1;
          } else if (!existing.location && job.location) {
            existing.location = job.location;
          }
        }
        if (!pageJobs.length || (pageNumber > 1 && added === 0)) break;
      }
    } finally {
      await browser.close();
    }
    if (urlIndex + 1 < urls.length) {
      await new Promise((resolve) => setTimeout(resolve, waitMs));
    }
  }

  process.stdout.write(`${JSON.stringify({ jobs: Array.from(jobs.values()) })}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message || error}\n`);
  process.exitCode = 1;
});
