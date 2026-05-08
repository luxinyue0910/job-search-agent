#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const readline = require("readline/promises");
const { chromium } = require("playwright");

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const current = argv[index];
    if (!current.startsWith("--")) continue;
    const key = current.slice(2);
    const next = argv[index + 1];
    if (!next || next.startsWith("--")) {
      args[key] = true;
    } else {
      args[key] = next;
      index += 1;
    }
  }
  return args;
}

function slug(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function personRoot(root, person) {
  const privateBase = process.env.JOB_SEARCH_PRIVATE_DIR
    ? path.resolve(process.env.JOB_SEARCH_PRIVATE_DIR)
    : root;
  const selected = slug(person || process.env.JOB_SEARCH_PERSON || "default");
  const defaultProfile = path.join(privateBase, "profiles", "default");
  if (selected === "default" && !fs.existsSync(defaultProfile)) {
    return { person: selected, dir: privateBase };
  }
  return { person: selected, dir: path.join(privateBase, "profiles", selected) };
}

function readJson(filePath, fallback) {
  if (!fs.existsSync(filePath)) return fallback;
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function defaultUrl(provider) {
  if (provider === "wellfound") return "https://wellfound.com/jobs";
  if (provider === "jobright") return "https://jobright.ai/jobs";
  throw new Error(`Unsupported provider: ${provider}`);
}

function normalizeText(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function isNoiseLine(line) {
  return (
    !line ||
    /^(apply|save|new|easy apply|view job|posted|promoted|be an early applicant|why this job is a match|more|less|share)$/i.test(line) ||
    /^\d+%$/.test(line) ||
    /^\/$/.test(line) ||
    /^\d+\s+(minute|hour|day|week|month)s?\s+ago$/i.test(line) ||
    /^(artificial intelligence|software|logistics|advertising|digital media|public company|early stage)(\s*[·|].*)?$/i.test(line)
  );
}

function companyFromProse(text) {
  const match = normalizeText(text).match(/\b([A-Z][A-Za-z0-9.&'-]+(?:\s+[A-Z][A-Za-z0-9.&'-]+){0,4})\s+is\s+(?:an?|the)\b/);
  if (!match) return "";
  const company = match[1].trim();
  return isPlausibleCompany(company) ? company : "";
}

function isPlausibleCompany(company) {
  if (!company) return false;
  if (company.length < 2 || company.length > 80) return false;
  if (isNoiseLine(company)) return false;
  if (/^(remote|seattle|bellevue|san francisco|san jose|california|washington|united states)$/i.test(company)) return false;
  if (/^(software|backend|frontend|full stack|machine learning|ai|data)\b/i.test(company)) return false;
  return /[A-Za-z]/.test(company);
}

function inferCompanyAndRole(text) {
  const lines = String(text || "")
    .split(/\n+/)
    .map((line) => normalizeText(line))
    .filter(Boolean)
    .filter((line) => !isNoiseLine(line));
  const roleLine = lines.find((line) => /engineer|developer|software|backend|frontend|full.?stack|platform|devops|machine learning|ai|data/i.test(line));
  const roleIndex = roleLine ? lines.indexOf(roleLine) : -1;
  let company = "";
  if (roleIndex > 0 && isPlausibleCompany(lines[roleIndex - 1])) company = lines[roleIndex - 1];
  if (!company && roleIndex >= 0 && isPlausibleCompany(lines[roleIndex + 1])) company = lines[roleIndex + 1];
  if (!company) company = companyFromProse(text);
  if (!company) company = lines.find((line) => isPlausibleCompany(line) && /^[A-Z][A-Za-z0-9 .,&'-]{1,80}$/.test(line)) || "";
  return {
    company: company.slice(0, 100),
    role: (roleLine || lines[0] || "").slice(0, 160),
  };
}

async function extractVisibleLeads(page, provider, sourceUrl) {
  const raw = await page.evaluate(() => {
    const isVisible = (node) => {
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const anchors = Array.from(document.querySelectorAll("a[href*='/jobs/info/'], a[href*='jobright.ai/jobs/info']"));
    const containers = anchors.length
      ? anchors.map((anchor) => anchor.closest("article, li, [data-testid*='job'], [class*='job'], [class*='Job'], div") || anchor)
      : Array.from(document.querySelectorAll("article, li, [data-testid*='job'], [class*='job'], [class*='Job']"));
    return containers
      .filter(isVisible)
      .map((node) => {
        const text = node.innerText || "";
        const links = Array.from(node.querySelectorAll("a[href]")).map((link) => link.href);
        return { text, links };
      })
      .filter((item) => {
        const text = item.text.replace(/\s+/g, " ").trim();
        return text.length >= 40 && text.length <= 1400 && /engineer|developer|software|backend|frontend|platform|devops|machine learning|ai|data/i.test(text);
      })
      .slice(0, 300);
  });

  const unique = new Map();
  for (const item of raw) {
    const text = normalizeText(item.text);
    const { company, role } = inferCompanyAndRole(item.text);
    if (!isPlausibleCompany(company) || !role) continue;
    const key = `${company.toLowerCase()}|${role.toLowerCase()}|${text.slice(0, 80)}`;
    if (unique.has(key)) continue;
    unique.set(key, {
      provider,
      company,
      role,
      location: inferLocation(text),
      aggregator_url: (item.links || []).find((link) => link.includes(provider)) || item.links?.[0] || sourceUrl,
      links: item.links || [],
      raw_text: text,
      captured_at: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
    });
  }
  return Array.from(unique.values());
}

function inferLocation(text) {
  const match = text.match(/\b(Remote|Seattle|Bellevue|San Francisco|SF|San Jose|Mountain View|Palo Alto|Sunnyvale|Redmond|Menlo Park|California|Washington|CA|WA)\b(?:,\s*[A-Z]{2})?/i);
  return match ? match[0] : "";
}

function detectPlatform(url) {
  const lower = String(url || "").toLowerCase();
  if (lower.includes("greenhouse.io")) return "greenhouse";
  if (lower.includes("lever.co")) return "lever";
  if (lower.includes("ashbyhq.com")) return "ashby";
  return "";
}

function sourceFromAtsUrl(company, url) {
  const parsed = new URL(url);
  const platform = detectPlatform(url);
  const genericBoards = new Set(["blog", "careers", "compare", "product-updates", "range", "alternative"]);
  if (platform === "lever") {
    const board = parsed.pathname.split("/").filter(Boolean)[0];
    if (!board || genericBoards.has(board.toLowerCase())) return null;
    return { company, platform, board, url: `https://jobs.lever.co/${board}` };
  }
  if (platform === "ashby") {
    const board = parsed.pathname.split("/").filter(Boolean)[0];
    if (!board || genericBoards.has(board.toLowerCase())) return null;
    return { company, platform, board, url: `https://jobs.ashbyhq.com/${board}` };
  }
  if (platform === "greenhouse") {
    const parts = parsed.pathname.split("/").filter(Boolean);
    let board = "";
    if (parsed.hostname === "boards.greenhouse.io") board = parts[0] || "";
    if (parsed.hostname === "job-boards.greenhouse.io") board = parts[0] || "";
    if (!board && parts.includes("jobs")) board = parts[0] || "";
    if (!board || genericBoards.has(board.toLowerCase())) return null;
    return { company, platform, board, url: `https://job-boards.greenhouse.io/${board}` };
  }
  return null;
}

async function searchOfficialSources(lead, limit) {
  const apiKey = process.env.SERPAPI_API_KEY;
  if (!apiKey || !isPlausibleCompany(lead.company)) return [];
  const query = `${lead.company} ${lead.role || ""} careers Greenhouse Lever Ashby`;
  const params = new URLSearchParams({
    engine: "google",
    q: query,
    api_key: apiKey,
    num: String(Math.min(limit, 20)),
  });
  const response = await fetch(`https://serpapi.com/search.json?${params.toString()}`);
  if (!response.ok) throw new Error(`SerpAPI failed for ${lead.company}: ${response.status}`);
  const data = await response.json();
  return (data.organic_results || [])
    .map((item) => item.link)
    .filter((url) => detectPlatform(url))
    .map((url) => sourceFromAtsUrl(lead.company, url))
    .filter(Boolean);
}

function mergeSources(privateRoot, sources) {
  const sourcesPath = path.join(privateRoot, "data", "sources.json");
  const data = readJson(sourcesPath, { sources: [] });
  const existing = new Set((data.sources || []).map((source) => `${source.platform}|${source.url}`));
  let added = 0;
  for (const source of sources) {
    const key = `${source.platform}|${source.url}`;
    if (existing.has(key)) continue;
    data.sources.push(source);
    existing.add(key);
    added += 1;
  }
  data.sources.sort((a, b) => String(a.company || "").localeCompare(String(b.company || "")));
  writeJson(sourcesPath, data);
  return added;
}

async function main() {
  const args = parseArgs(process.argv);
  const provider = args.provider || "jobright";
  const root = path.resolve(__dirname, "..");
  const selected = personRoot(root, args.person);
  const outPath = args.out
    ? path.resolve(args.out)
    : path.join(selected.dir, "data", "aggregator_leads.json");
  const url = args.url || defaultUrl(provider);
  const maxScrolls = Number(args["max-scrolls"] || 8);
  const resolveSources = Boolean(args["resolve-sources"]);
  const searchLimit = Number(args["search-results-per-lead"] || 5);
  const userDataDir = path.join(selected.dir, ".browser-profile", selected.person, "aggregators", provider);

  const context = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });

  console.log(`Opened ${url}`);
  console.log("Log in if needed, set filters, and leave the results page visible.");
  console.log("Recommended filters: Software/Backend/AI, WA/CA/Remote, posted last 24h or 7d.");
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  await rl.question("Press Enter here when the page is ready to capture...");
  rl.close();

  for (let index = 0; index < maxScrolls; index += 1) {
    await page.mouse.wheel(0, 1800);
    await page.waitForTimeout(1200);
  }

  const leads = await extractVisibleLeads(page, provider, page.url());
  const previous = readJson(outPath, { leads: [] });
  const merged = new Map();
  for (const lead of previous.leads || []) merged.set(`${lead.provider}|${lead.company}|${lead.role}|${lead.aggregator_url}`, lead);
  for (const lead of leads) merged.set(`${lead.provider}|${lead.company}|${lead.role}|${lead.aggregator_url}`, lead);
  const output = { leads: Array.from(merged.values()) };
  writeJson(outPath, output);
  console.log(`Captured ${leads.length} visible leads. Total stored: ${output.leads.length}.`);
  console.log(`Wrote ${outPath}`);

  if (resolveSources) {
    const sources = [];
    for (const lead of leads) {
      try {
        sources.push(...(await searchOfficialSources(lead, searchLimit)));
      } catch (error) {
        console.error(`Could not resolve ${lead.company || lead.role}: ${error.message}`);
      }
    }
    const added = mergeSources(selected.dir, sources);
    console.log(`Resolved ${sources.length} candidate ATS sources. Added ${added} new sources.`);
  }

  console.log("Browser is staying open for review. Press Ctrl+C when done.");
  await new Promise(() => {});
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
