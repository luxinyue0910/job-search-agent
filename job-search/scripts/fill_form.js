#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const current = argv[index];
    if (current.startsWith("--")) {
      const key = current.slice(2);
      const next = argv[index + 1];
      if (!next || next.startsWith("--")) {
        args[key] = true;
      } else {
        args[key] = next;
        index += 1;
      }
    }
  }
  return args;
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
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

function writeJson(filePath, data) {
  fs.writeFileSync(filePath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
}

function writeCsv(root, tracker) {
  const fields = [
    "id", "company", "role", "url", "platform", "location", "status", "fit_score", "ats_score",
    "date_found", "date_applied", "resume_path", "cover_letter_path", "screenshot_path", "notes"
  ];
  const rows = [fields.join(",")];
  for (const app of tracker.applications || []) {
    rows.push(fields.map((field) => csvCell(app[field] || "")).join(","));
  }
  fs.writeFileSync(path.join(root, "data", "applications.csv"), `${rows.join("\n")}\n`, "utf8");
}

function csvCell(value) {
  const text = String(value);
  if (/[",\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function findApplication(tracker, id) {
  return tracker.applications.find((app) => app.id === id || app.url === id);
}

async function fillFirst(page, selectors, value) {
  if (!value) return false;
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    try {
      if (await locator.count()) {
        await locator.fill(String(value), { timeout: 3000 });
        return true;
      }
    } catch (_error) {
      // Try the next likely selector.
    }
  }
  return false;
}

async function chooseCountry(page, country) {
  const nativeSelect = page.locator('select[name*="phone" i], select[aria-label*="country" i], select[name*="country" i]').first();
  try {
    if (await nativeSelect.count()) {
      await nativeSelect.selectOption({ label: country }, { timeout: 3000 });
      return true;
    }
  } catch (_error) {
    // Try custom dropdowns below.
  }

  for (const selector of ['[aria-label*="Country" i]', '[data-testid*="country" i]']) {
    const locator = page.locator(selector).first();
    try {
      if (await locator.count()) {
        await locator.click({ timeout: 3000 });
        await page.getByText(country, { exact: true }).first().click({ timeout: 3000 });
        return true;
      }
    } catch (_error) {
      // Try next selector.
    }
  }
  return false;
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.id) {
    throw new Error("Usage: node scripts/fill_form.js --id <application-id>");
  }

  const root = path.resolve(__dirname, "..");
  const selected = personRoot(root, args.person);
  const profile = readJson(path.join(selected.dir, "profile.json"));
  const trackerPath = path.join(selected.dir, "data", "applications.json");
  const tracker = readJson(trackerPath);
  const app = findApplication(tracker, args.id);
  if (!app) throw new Error(`No application found for ${args.id}`);

  const { chromium } = require("playwright");
  const userDataDir = path.join(selected.dir, ".browser-profile", selected.person, "ats");
  const context = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    viewport: { width: 1400, height: 950 }
  });
  const page = context.pages()[0] || await context.newPage();

  const outputDir = path.join(selected.dir, "output", slug(app.company), slug(app.role));
  fs.mkdirSync(outputDir, { recursive: true });
  const screenshotPath = path.join(outputDir, "pre_submit.png");
  const actionItems = new Set(app.action_items || []);

  try {
    await page.goto(app.url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(2500);
    const visibleText = (await page.locator("body").innerText({ timeout: 10000 })).toLowerCase();

    if (/captcha|verify you are human|sign in|log in|create account|e-signature|signature/.test(visibleText)) {
      actionItems.add("Browser stopped for login/CAPTCHA/account/signature step.");
      await page.screenshot({ path: screenshotPath, fullPage: true });
      updateApp(tracker, app.id, { status: "needs_review", screenshot_path: screenshotPath, action_items: [...actionItems] });
      writeJson(trackerPath, tracker);
      writeCsv(selected.dir, tracker);
      console.log(`Stopped for manual review. Screenshot: ${screenshotPath}`);
      return;
    }

    const personal = profile.personal || {};
    const links = profile.links || {};
    const defaults = profile.application_defaults || {};
    await fillFirst(page, ['input[name*="first" i]', 'input[aria-label*="first" i]', 'input[id*="first" i]'], firstName(personal.name));
    await fillFirst(page, ['input[name*="last" i]', 'input[aria-label*="last" i]', 'input[id*="last" i]'], lastName(personal.name));
    await fillFirst(page, ['input[name*="preferred" i]', 'input[aria-label*="preferred" i]', 'input[id*="preferred" i]'], firstName(personal.name));
    await fillFirst(page, ['input[type="email"]', 'input[name*="email" i]', 'input[aria-label*="email" i]'], personal.email);
    await fillFirst(page, ['input[type="tel"]', 'input[name*="phone" i]', 'input[aria-label*="phone" i]'], personal.phone);
    await fillFirst(page, ['input[name*="location" i]', 'input[aria-label*="location" i]', 'input[id*="location" i]'], personal.location);
    await fillFirst(page, ['input[name*="linkedin" i]', 'input[aria-label*="linkedin" i]', 'input[id*="linkedin" i]'], links.linkedin);
    await fillFirst(page, ['input[name*="website" i]', 'input[aria-label*="website" i]', 'input[id*="website" i]', 'input[name*="portfolio" i]', 'input[aria-label*="portfolio" i]'], links.website);
    await chooseCountry(page, personal.country || "United States");

    const resumePath = resolveWorkspacePath(root, selected.dir, profile.resume_file || app.resume_path || "");
    if (resumePath && fs.existsSync(resumePath)) {
      const fileInputs = await page.locator('input[type="file"]').all();
      if (fileInputs.length > 0) {
        await fileInputs[0].setInputFiles(resumePath);
      }
    } else {
      actionItems.add("Resume upload skipped because resume_path did not point to an existing file.");
    }

    await fillFirst(page, ['textarea[name*="cover" i]', 'textarea[aria-label*="cover" i]'], coverLetterText(app.cover_letter_path, root));
    await fillFirst(page, ['input[name*="authorized" i]', 'textarea[name*="authorized" i]'], defaults.authorized_to_work);
    await fillFirst(page, ['input[name*="sponsor" i]', 'textarea[name*="sponsor" i]'], defaults.requires_sponsorship);

    actionItems.add("Review all fields manually. Final application submit must be clicked by you, not automation.");
    await page.screenshot({ path: screenshotPath, fullPage: true });
    updateApp(tracker, app.id, { status: "needs_review", screenshot_path: screenshotPath, action_items: [...actionItems] });
    writeJson(trackerPath, tracker);
    writeCsv(selected.dir, tracker);
    console.log(`Filled conservative fields and stopped before submit. Screenshot: ${screenshotPath}`);
  } finally {
    if (args["keep-open"]) {
      console.log("Browser is staying open for your manual review. Press Ctrl+C in this terminal after you finish.");
      await new Promise(() => {});
    } else {
      console.log("Browser remains visible only during this run; rerun if you need another pass.");
      await context.close();
    }
  }
}

function updateApp(tracker, id, updates) {
  const index = tracker.applications.findIndex((item) => item.id === id);
  if (index >= 0) {
    tracker.applications[index] = { ...tracker.applications[index], ...updates };
    tracker.last_updated = new Date().toISOString();
  }
}

function slug(value) {
  return String(value || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "unknown";
}

function firstName(name) {
  return String(name || "").trim().split(/\s+/)[0] || "";
}

function lastName(name) {
  const parts = String(name || "").trim().split(/\s+/);
  return parts.length > 1 ? parts.slice(1).join(" ") : "";
}

function coverLetterText(relativePath, root) {
  if (!relativePath) return "";
  const fullPath = path.isAbsolute(relativePath) ? relativePath : path.resolve(root, "..", relativePath);
  if (!fs.existsSync(fullPath)) return "";
  return fs.readFileSync(fullPath, "utf8");
}

function resolveWorkspacePath(root, selectedDir, value) {
  if (!value) return "";
  if (path.isAbsolute(value)) return value;
  const personRelative = path.resolve(selectedDir, value);
  if (fs.existsSync(personRelative)) return personRelative;
  return path.resolve(root, "..", value);
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
