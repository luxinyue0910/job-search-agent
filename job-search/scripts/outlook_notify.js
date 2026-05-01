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

function slug(value) {
  return String(value || "default").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "default";
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

function parseSummary(markdown) {
  const lines = markdown.split(/\r?\n/);
  const subjectLine = lines.find((line) => line.startsWith("Subject:"));
  const subject = subjectLine ? subjectLine.replace(/^Subject:\s*/, "").trim() : "Job Search Summary";
  const body = lines.filter((line) => !line.startsWith("Subject:")).join("\n").trim();
  return { subject, body };
}

async function clickFirst(page, locators, timeout = 15000) {
  for (const locator of locators) {
    try {
      await locator.first().click({ timeout });
      return true;
    } catch (_error) {
      // Try next Outlook variant.
    }
  }
  return false;
}

async function fillFirst(page, locators, value, timeout = 15000) {
  for (const locator of locators) {
    try {
      await locator.first().fill(value, { timeout });
      return true;
    } catch (_error) {
      // Try next Outlook variant.
    }
  }
  return false;
}

async function main() {
  const args = parseArgs(process.argv);
  const root = path.resolve(__dirname, "..");
  const selected = personRoot(root, args.person);
  const profilePath = path.join(selected.dir, "profile.json");
  const summaryPath = path.resolve(args.summary || path.join(selected.dir, "output", "notifications", "latest.md"));
  const profile = readJson(profilePath);
  const email = profile.personal && profile.personal.email;
  const notifications = profile.notifications || {};

  if (!notifications.email_enabled || notifications.mode !== "self_summary_only") {
    throw new Error("Email notification is disabled or not in self_summary_only mode.");
  }
  if ((notifications.provider || "outlook") !== "outlook") {
    throw new Error("profile.notifications.provider is not outlook.");
  }
  if (!email || /example\.com$/i.test(email)) {
    throw new Error("Set profile.personal.email to your real Outlook address before sending notifications.");
  }

  const markdown = fs.readFileSync(summaryPath, "utf8");
  const { subject, body } = parseSummary(markdown);
  const { chromium } = require("playwright");
  const userDataDir = path.join(selected.dir, ".browser-profile", selected.person, "outlook");
  const context = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    viewport: { width: 1280, height: 900 }
  });
  const page = context.pages()[0] || await context.newPage();

  try {
    await page.goto("https://outlook.office.com/mail/", { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(4000);

    if (/login\.|signin|oauth|live\.com\/login|microsoftonline\.com/i.test(page.url())) {
      throw new Error("Outlook requires login. Log in manually in the opened browser, then rerun this command.");
    }

    const newMailClicked = await clickFirst(page, [
      page.getByRole("button", { name: /new mail|new message|compose/i }),
      page.locator('button[aria-label*="New mail" i]'),
      page.locator('button[aria-label*="New message" i]')
    ]);
    if (!newMailClicked) throw new Error("Could not find Outlook New mail button.");

    const toFilled = await fillFirst(page, [
      page.locator('div[role="textbox"][aria-label*="To" i]'),
      page.locator('input[aria-label*="To" i]'),
      page.locator('div[contenteditable="true"][aria-label*="To" i]')
    ], email);
    if (!toFilled) throw new Error("Could not fill Outlook To field.");
    await page.keyboard.press("Enter");

    const subjectFilled = await fillFirst(page, [
      page.locator('input[aria-label*="subject" i]'),
      page.locator('input[placeholder*="subject" i]')
    ], subject);
    if (!subjectFilled) throw new Error("Could not fill Outlook subject field.");

    const bodyFilled = await fillFirst(page, [
      page.locator('div[aria-label*="Message body" i]'),
      page.locator('div[role="textbox"][aria-label*="body" i]'),
      page.locator('div[contenteditable="true"][aria-label*="Message body" i]')
    ], body);
    if (!bodyFilled) throw new Error("Could not fill Outlook message body.");

    const bodyText = await page.locator("body").innerText({ timeout: 10000 });
    if (!bodyText.toLowerCase().includes(email.toLowerCase())) {
      throw new Error("Refusing to send: could not verify self recipient on the compose page.");
    }

    const sendClicked = await clickFirst(page, [
      page.getByRole("button", { name: /^send$/i }),
      page.locator('button[aria-label^="Send" i]')
    ]);
    if (!sendClicked) throw new Error("Could not find Outlook Send button.");

    await page.waitForTimeout(2500);
    console.log(`Sent Outlook notification to ${email}`);
  } catch (error) {
    console.error(`Outlook notification failed: ${error.message}`);
    console.error(`Notification markdown remains at ${summaryPath}`);
    process.exitCode = 2;
  } finally {
    await context.close();
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
