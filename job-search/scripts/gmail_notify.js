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

async function main() {
  const args = parseArgs(process.argv);
  const root = path.resolve(__dirname, "..");
  const selected = personRoot(root, args.person);
  const profilePath = path.join(selected.dir, "profile.json");
  const summaryPath = path.resolve(args.summary || path.join(selected.dir, "output", "notifications", "latest.md"));
  const profile = readJson(profilePath);
  const email = profile.personal && profile.personal.email;
  const notifications = profile.notifications || {};

  if (!notifications.gmail_enabled || notifications.mode !== "self_summary_only") {
    throw new Error("Gmail notification is disabled or not in self_summary_only mode.");
  }
  if (!email || /example\.com$/i.test(email)) {
    throw new Error("Set profile.personal.email to your real Gmail address before sending notifications.");
  }

  const markdown = fs.readFileSync(summaryPath, "utf8");
  const { subject, body } = parseSummary(markdown);
  const { chromium } = require("playwright");
  const userDataDir = path.join(selected.dir, ".browser-profile", selected.person, "gmail");
  const context = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    viewport: { width: 1280, height: 900 }
  });

  const page = context.pages()[0] || await context.newPage();
  try {
    await page.goto("https://mail.google.com/mail/u/0/#inbox", { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(3000);

    if (/accounts\.google\.com/.test(page.url())) {
      throw new Error("Gmail requires login. Log in manually in the opened browser, then rerun this command.");
    }

    const compose = page.getByText(/^Compose$/).first();
    await compose.click({ timeout: 15000 });

    const toBox = page.locator('textarea[name="to"], input[aria-label*="To"], textarea[aria-label*="To"]').first();
    await toBox.fill(email, { timeout: 15000 });
    await page.keyboard.press("Enter");

    const subjectBox = page.locator('input[name="subjectbox"]').first();
    await subjectBox.fill(subject, { timeout: 15000 });

    const bodyBox = page.locator('div[aria-label="Message Body"], div[role="textbox"][aria-label*="Message Body"]').first();
    await bodyBox.fill(body, { timeout: 15000 });

    const recipients = await page.locator('span[email], div[email]').evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("email")).filter(Boolean)
    ).catch(() => []);
    const uniqueRecipients = [...new Set(recipients.map((item) => item.toLowerCase()))];
    if (uniqueRecipients.length > 1 || (uniqueRecipients.length === 1 && uniqueRecipients[0] !== email.toLowerCase())) {
      throw new Error(`Refusing to send: recipients are not self-only (${uniqueRecipients.join(", ")}).`);
    }

    const sendButton = page.locator('div[role="button"][aria-label*="Send"], div[role="button"][data-tooltip*="Send"]').first();
    await sendButton.click({ timeout: 15000 });
    await page.waitForTimeout(2500);
    console.log(`Sent Gmail notification to ${email}`);
  } catch (error) {
    console.error(`Gmail notification failed: ${error.message}`);
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
