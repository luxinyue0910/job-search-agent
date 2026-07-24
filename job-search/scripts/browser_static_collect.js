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

function validateUrl(value) {
  const parsed = new URL(String(value || ""));
  const host = parsed.hostname.toLowerCase();
  if (parsed.protocol !== "https:") {
    throw new Error(`Browser static sources must use HTTPS: ${parsed.toString()}`);
  }
  if (
    host === "localhost" ||
    host === "0.0.0.0" ||
    host === "::1" ||
    host.startsWith("127.") ||
    host.startsWith("169.254.")
  ) {
    throw new Error(`Browser static source host is not allowed: ${host}`);
  }
  return parsed.toString();
}

function replaceTemplate(template, id) {
  return String(template || "").replaceAll("{id}", encodeURIComponent(id));
}

async function loadPage(page, url, source) {
  const timeoutMs = Math.max(5000, Number(source.browser_timeout_ms || 25000));
  const attempts = Math.max(1, Math.min(Number(source.browser_retries || 2) + 1, 4));
  const waitMs = Math.max(250, Number(source.browser_wait_ms || 1800));
  let lastError = null;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
      if (source.ready_selector) {
        await page.locator(String(source.ready_selector)).first().waitFor({
          state: "attached",
          timeout: timeoutMs,
        });
      }
      await page.waitForTimeout(waitMs);
      const title = await page.title();
      const bodyText = normalizeText(await page.locator("body").innerText());
      const challenged =
        /just a moment|attention required|error 403 forbidden/i.test(title) ||
        /performing security verification|verify you are human|403 forbidden/i.test(bodyText);
      if (challenged) {
        throw new Error(`Browser security verification blocked ${url}.`);
      }
      return;
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await page.waitForTimeout(waitMs * attempt);
    }
  }
  throw lastError || new Error(`Could not load ${url}`);
}

async function extractItemJobs(page, source, sourceUrl) {
  const config = {
    itemSelector: String(source.item_selector || ""),
    titleSelector: String(source.title_selector || ""),
    linkSelector: String(source.link_selector || ""),
    locationSelector: String(source.location_selector || ""),
    locationAttribute: String(source.location_attribute || ""),
    idAttribute: String(source.id_attribute || ""),
    idSelector: String(source.id_selector || ""),
    dateSelector: String(source.date_selector || ""),
    dateRegex: String(source.date_regex || ""),
    detailSelectorTemplate: String(source.detail_selector_template || ""),
    urlTemplate: String(source.url_template || ""),
    roleStripRegex: String(source.role_strip_regex || ""),
    defaultLocation: String(source.default_location || ""),
    sourceUrl,
  };
  return page.locator(config.itemSelector).evaluateAll((items, options) => {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const replace = (template, id) =>
      String(template || "").replaceAll("{id}", encodeURIComponent(id));
    const datePattern = options.dateRegex ? new RegExp(options.dateRegex, "i") : null;
    const rolePattern = options.roleStripRegex
      ? new RegExp(options.roleStripRegex, "i")
      : null;

    return items
      .map((item) => {
        const idNode = options.idSelector ? item.querySelector(options.idSelector) : null;
        const id = clean(
          (options.idAttribute ? item.getAttribute(options.idAttribute) : idNode?.textContent) || "",
        );
        const link = options.linkSelector ? item.querySelector(options.linkSelector) : null;
        const titleNode = options.titleSelector
          ? item.querySelector(options.titleSelector)
          : link;
        let role = clean(titleNode?.textContent || link?.textContent || "");
        if (rolePattern) role = clean(role.replace(rolePattern, ""));
        const url = link?.href || (options.urlTemplate && id
          ? replace(options.urlTemplate, id)
          : "");
        const locationNode = options.locationSelector
          ? item.querySelector(options.locationSelector)
          : null;
        const location = clean(
          (options.locationAttribute
            ? item.getAttribute(options.locationAttribute)
            : locationNode?.textContent) || options.defaultLocation,
        );
        const dateNode = options.dateSelector
          ? item.querySelector(options.dateSelector)
          : null;
        const dateText = clean(dateNode?.textContent || "");
        const postedAt = datePattern
          ? clean((dateText.match(datePattern) || [])[1] || "")
          : dateText;
        const detailSelector = options.detailSelectorTemplate && id
          ? replace(options.detailSelectorTemplate, id)
          : "";
        const detailNode = detailSelector ? document.querySelector(detailSelector) : null;
        return {
          role,
          url,
          location,
          posted_at: postedAt,
          external_job_id: id,
          description: clean(detailNode?.innerText || ""),
          source_url: options.sourceUrl,
        };
      })
      .filter((job) => job.role && job.url);
  }, config);
}

async function extractLinkJobs(page, source, sourceUrl) {
  const selector = String(source.link_selector || "");
  const config = {
    roleStripRegex: String(source.role_strip_regex || ""),
    idRegex: String(source.id_regex || ""),
    defaultLocation: String(source.default_location || ""),
    sourceUrl,
  };
  return page.locator(selector).evaluateAll((links, options) => {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const rolePattern = options.roleStripRegex
      ? new RegExp(options.roleStripRegex, "i")
      : null;
    const idPattern = options.idRegex ? new RegExp(options.idRegex, "i") : null;
    return links
      .map((link) => {
        let role = clean(link.textContent || "");
        if (rolePattern) role = clean(role.replace(rolePattern, ""));
        const url = link.href || "";
        const idMatch = idPattern ? url.match(idPattern) : null;
        return {
          role,
          url,
          location: options.defaultLocation,
          posted_at: "",
          external_job_id: clean(idMatch?.[1] || ""),
          description: "",
          source_url: options.sourceUrl,
        };
      })
      .filter((job) => job.role && job.url);
  }, config);
}

async function main() {
  const source = readSource();
  const sourceUrl = validateUrl(source.url);
  if (!source.item_selector && !source.link_selector) {
    throw new Error("Configure item_selector or link_selector for browser_static.");
  }
  const headless = truthy(source.browser_headless, true);
  const browser = await chromium.launch({
    channel: String(source.browser_channel || "chrome"),
    headless,
    args: headless ? [] : ["--window-position=-10000,-10000", "--window-size=100,100"],
  });
  try {
    const contextOptions = {};
    if (source.browser_user_agent) {
      contextOptions.userAgent = String(source.browser_user_agent);
    }
    const context = await browser.newContext(contextOptions);
    const page = await context.newPage();
    await loadPage(page, sourceUrl, source);
    const unique = new Map();
    const scrollSelector = String(source.scroll_selector || "");
    const maxScrolls = scrollSelector
      ? Math.max(1, Math.min(Number(source.max_scrolls || 4), 20))
      : 1;
    const scrollWaitMs = Math.max(250, Number(source.scroll_wait_ms || 1000));
    for (let scrollIndex = 0; scrollIndex < maxScrolls; scrollIndex += 1) {
      const pageJobs = source.item_selector
        ? await extractItemJobs(page, source, sourceUrl)
        : await extractLinkJobs(page, source, sourceUrl);
      for (const job of pageJobs) {
        const normalizedUrl = validateUrl(job.url);
        unique.set(normalizedUrl, { ...job, url: normalizedUrl });
      }
      if (!scrollSelector) break;
      const scrollNode = page.locator(scrollSelector).first();
      if (!(await scrollNode.count())) {
        throw new Error(`Configured scroll_selector was not found: ${scrollSelector}`);
      }
      const before = await scrollNode.evaluate((element) => ({
        top: element.scrollTop,
        height: element.scrollHeight,
        client: element.clientHeight,
      }));
      await scrollNode.evaluate((element) => {
        element.scrollTop = element.scrollHeight;
        element.dispatchEvent(new Event("scroll", { bubbles: true }));
      });
      await page.waitForTimeout(scrollWaitMs);
      const after = await scrollNode.evaluate((element) => ({
        top: element.scrollTop,
        height: element.scrollHeight,
        client: element.clientHeight,
      }));
      if (
        scrollIndex > 0 &&
        before.top === after.top &&
        before.height === after.height &&
        before.client === after.client
      ) {
        break;
      }
    }
    if (truthy(source.empty_is_failure, false) && unique.size === 0) {
      throw new Error(`No configured job elements were found at ${sourceUrl}.`);
    }
    process.stdout.write(`${JSON.stringify({ jobs: Array.from(unique.values()) })}\n`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message || error}\n`);
  process.exitCode = 1;
});
