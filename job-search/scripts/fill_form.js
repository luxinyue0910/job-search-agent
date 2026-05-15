#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

const FILL_TIMEOUT = 900;
const NAVIGATION_TIMEOUT = 5000;
const DEFAULT_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

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
        await locator.fill(String(value), { timeout: FILL_TIMEOUT });
        return true;
      }
    } catch (_error) {
      // Try the next likely selector.
    }
  }
  return false;
}

async function fillByLabel(page, labelPattern, value) {
  if (!value) return false;
  try {
    const locator = page.getByLabel(labelPattern).first();
    if (await locator.count()) {
      await locator.fill(String(value), { timeout: FILL_TIMEOUT });
      return true;
    }
  } catch (_error) {
    // Try label containers below.
  }

  const field = fieldByLabel(page, labelPattern);
  try {
    if (await field.count()) {
      const input = field.locator('input:not([type="hidden"]), textarea').first();
      if (await input.count()) {
        await input.fill(String(value), { timeout: FILL_TIMEOUT });
        return true;
      }
    }
  } catch (_error) {
    // Leave for manual review.
  }
  return false;
}

async function selectByLabel(page, labelPattern, preferredLabels) {
  const labels = Array.isArray(preferredLabels) ? preferredLabels.filter(Boolean) : [preferredLabels].filter(Boolean);
  if (!labels.length) return false;

  const direct = page.getByLabel(labelPattern).first();
  try {
    if (await direct.count()) {
      const tagName = await direct.evaluate((node) => node.tagName.toLowerCase()).catch(() => "");
      if (tagName === "select") {
        for (const label of labels) {
          try {
            await direct.selectOption({ label }, { timeout: FILL_TIMEOUT });
            return true;
          } catch (_error) {
            // Try next label.
          }
        }
      }
    }
  } catch (_error) {
    // Try custom controls below.
  }

  const field = fieldByLabel(page, labelPattern);
  for (const label of labels) {
    try {
      if (!(await field.count())) continue;
      await field.locator('input[role="combobox"], input, [role="combobox"], button').first().click({ timeout: FILL_TIMEOUT });
      const input = field.locator('input[role="combobox"], input:not([type="hidden"])').first();
      if (await input.count()) {
        await input.fill(String(label), { timeout: FILL_TIMEOUT });
      }
      await page.getByRole("option", { name: new RegExp(escapeRegExp(label), "i") }).first().click({ timeout: FILL_TIMEOUT });
      return true;
    } catch (_error) {
      try {
        await keyboardFor(page).press("Escape");
      } catch (_keyboardError) {
        // Ignore.
      }
    }
  }

  for (const label of labels) {
    try {
      if (!(await field.count())) continue;
      await field.getByText(new RegExp(`^${escapeRegExp(label)}$`, "i")).first().click({ timeout: FILL_TIMEOUT });
      return true;
    } catch (_error) {
      // Try next label.
    }
  }
  return false;
}

async function fillSchoolField(page, education, actionItems) {
  const school = education.school || "University of Colorado Boulder";
  const field = fieldByLabel(page, /^school|college|university|institution/i);
  if (!(await field.count())) return false;

  try {
    const nativeSelect = field.locator("select").first();
    if (await nativeSelect.count()) {
      await nativeSelect.selectOption({ label: school }, { timeout: FILL_TIMEOUT });
      return true;
    }
  } catch (_error) {
    // Fall through to text/combobox handling.
  }

  try {
    const input = field.locator('input[role="combobox"], input:not([type="hidden"]), textarea').first();
    if (await input.count()) {
      await input.click({ timeout: FILL_TIMEOUT });
      await input.fill(school, { timeout: FILL_TIMEOUT });
      try {
        await page.getByRole("option", { name: new RegExp(`^${escapeRegExp(school)}$`, "i") }).first().click({ timeout: FILL_TIMEOUT });
      } catch (_optionError) {
        actionItems.add("School field was filled as text only. Confirm it did not auto-select the wrong university.");
      }
      return true;
    }
  } catch (_error) {
    actionItems.add("School field needs manual review. Use University of Colorado Boulder.");
  }
  return false;
}

async function clickChoiceByLabel(page, labelPattern, preferredLabels) {
  const labels = Array.isArray(preferredLabels) ? preferredLabels.filter(Boolean) : [preferredLabels].filter(Boolean);
  if (!labels.length) return false;
  const field = fieldByLabel(page, labelPattern);
  for (const label of labels) {
    try {
      if (!(await field.count())) continue;
      const exact = new RegExp(`^${escapeRegExp(label)}$`, "i");
      await field.getByText(exact).first().click({ timeout: FILL_TIMEOUT });
      return true;
    } catch (_error) {
      // Try role-based control.
    }
    try {
      if (!(await field.count())) continue;
      await field.getByRole("radio", { name: new RegExp(escapeRegExp(label), "i") }).first().check({ timeout: FILL_TIMEOUT });
      return true;
    } catch (_error) {
      // Try checkbox.
    }
    try {
      if (!(await field.count())) continue;
      await field.getByRole("checkbox", { name: new RegExp(escapeRegExp(label), "i") }).first().check({ timeout: FILL_TIMEOUT });
      return true;
    } catch (_error) {
      // Try next label.
    }
  }
  return false;
}

function fieldByLabel(page, labelPattern) {
  const flags = labelPattern instanceof RegExp ? labelPattern.flags.replace("g", "") : "i";
  const source = labelPattern instanceof RegExp ? labelPattern.source : escapeRegExp(String(labelPattern));
  const regex = new RegExp(source, flags.includes("i") ? flags : `${flags}i`);
  return page.locator(".field-wrapper, .select, fieldset, .education--form").filter({ hasText: regex }).first();
}

async function chooseCountry(page, country) {
  const nativeSelect = page.locator('select[name*="phone" i], select[aria-label*="country" i], select[name*="country" i]').first();
  try {
    if (await nativeSelect.count()) {
      await nativeSelect.selectOption({ label: country }, { timeout: FILL_TIMEOUT });
      return true;
    }
  } catch (_error) {
    // Try custom dropdowns below.
  }

  for (const selector of ['[aria-label*="Country" i]', '[data-testid*="country" i]']) {
    const locator = page.locator(selector).first();
    try {
      if (await locator.count()) {
        await locator.click({ timeout: FILL_TIMEOUT });
        await page.getByText(country, { exact: true }).first().click({ timeout: FILL_TIMEOUT });
        return true;
      }
    } catch (_error) {
      // Try next selector.
    }
  }
  return false;
}

async function fillStructuredApplicationFields(page, profile, app, actionItems) {
  const personal = profile.personal || {};
  const education = profile.education || {};
  const defaults = profile.application_defaults || {};
  const workAuth = profile.work_authorization || {};
  const preferences = profile.preferences || {};

  await selectByLabel(page, /country/i, [personal.country || "United States", "United States"]);
  await selectByLabel(page, /location|candidate location|city/i, [
    personal.location,
    `${personal.city || ""}, ${personal.state || ""}`.trim().replace(/,\s*$/, ""),
    personal.city,
  ]);
  await fillByLabel(page, /location|candidate location|city/i, personal.location);
  await fillGreenhouseQuestionByLabel(page, /candidate location|location/i, personal.location);

  await fillSchoolField(page, education, actionItems);
  await selectByLabel(page, /^degree/i, [education.degree, "Master of Science", "Master's Degree", "Masters"]);
  await selectByLabel(page, /discipline|major/i, [education.major, "Computer Science"]);

  await clickChoiceByLabel(page, /personal pronouns|pronouns/i, ["She /Her", "She/Her", "She / Her"]);
  await fillByLabel(page, /current company/i, "Youmigo Tech");
  await fillByLabel(page, /current title/i, "Full-stack Software Engineer");
  await fillByLabel(page, /current visa status|visa status/i, defaults.current_visa_status || workAuth.status || "Green Card");
  await fillGreenhouseQuestionByLabel(page, /current visa status|basis of your current employment authorization/i, defaults.current_visa_status || workAuth.status || "Green Card");

  await clickChoiceByLabel(page, /authorized|legally authorized/i, ["Yes"]);
  await clickChoiceByLabel(page, /h-?1b|sponsorship|sponsor/i, ["No"]);
  await selectGreenhouseQuestionByLabel(page, /sponsorship|sponsor/i, ["No"]);

  await answerRelocationQuestions(page, app, preferences, defaults, actionItems);
  await clickChoiceByLabel(page, /acknowledge|confirm|agree/i, ["Yes, I acknowledge, agree, and confirm.", "Yes"]);
  await selectGreenhouseQuestionByLabel(page, /acknowledge|confirm|agree/i, ["Yes, I acknowledge, agree, and confirm.", "Yes"]);

  actionItems.add("Review EEO demographic fields manually: gender, Hispanic/Latino, race, veteran, disability, and sexual orientation.");
}

async function selectGreenhouseQuestionByLabel(page, labelPattern, preferredLabels) {
  const questions = await greenhouseQuestions(page);
  const question = questions.find((item) => labelPattern.test(item.label || item.name || ""));
  if (!question) return false;
  const labels = Array.isArray(preferredLabels) ? preferredLabels.filter(Boolean) : [preferredLabels].filter(Boolean);
  for (const field of question.fields || []) {
    const selector = field.name ? `[name="${cssEscape(field.name)}"], [id="${cssEscape(field.name)}"]` : "";
    if (!selector) continue;
    const target = page.locator(selector).first();
    for (const label of labels) {
      try {
        if (!(await target.count())) continue;
        const tagName = await target.evaluate((node) => node.tagName.toLowerCase()).catch(() => "");
        if (tagName === "select") {
          await target.selectOption({ label }, { timeout: FILL_TIMEOUT });
          return true;
        }
        await target.click({ timeout: FILL_TIMEOUT });
        await target.fill(String(label), { timeout: FILL_TIMEOUT }).catch(() => {});
        try {
          await page.getByRole("option", { name: new RegExp(escapeRegExp(label), "i") }).first().click({ timeout: FILL_TIMEOUT });
          return true;
        } catch (_optionError) {
          await keyboardFor(page).press("Enter");
          return true;
        }
      } catch (_error) {
        try {
          const fieldContainer = page.locator(".field-wrapper").filter({ has: target }).first();
          await fieldContainer.getByText(new RegExp(`^${escapeRegExp(label)}$`, "i")).first().click({ timeout: FILL_TIMEOUT });
          return true;
        } catch (_fallbackError) {
          // Try next label.
        }
      }
    }
  }
  return false;
}

async function answerDemographicQuestion(page, labelPattern, preferredLabels) {
  const labels = Array.isArray(preferredLabels) ? preferredLabels.filter(Boolean) : [preferredLabels].filter(Boolean);
  if (!labels.length) return false;
  if (await clickChoiceByLabel(page, labelPattern, labels)) return true;
  if (await selectByLabel(page, labelPattern, labels)) return true;
  if (await selectGreenhouseQuestionByLabel(page, labelPattern, labels)) return true;
  return clickChoiceNearLabel(page, labelPattern, labels);
}

async function clickChoiceNearLabel(page, labelPattern, preferredLabels) {
  const labels = Array.isArray(preferredLabels) ? preferredLabels.filter(Boolean) : [preferredLabels].filter(Boolean);
  const field = fieldByLabel(page, labelPattern);
  for (const label of labels) {
    const labelRegex = new RegExp(escapeRegExp(label), "i");
    try {
      if (!(await field.count())) continue;
      await field.getByText(labelRegex).first().click({ timeout: FILL_TIMEOUT });
      return true;
    } catch (_error) {
      // Try next control type.
    }
    try {
      if (!(await field.count())) continue;
      await field.locator("label").filter({ hasText: labelRegex }).first().click({ timeout: FILL_TIMEOUT });
      return true;
    } catch (_error) {
      // Try next control type.
    }
    try {
      if (!(await field.count())) continue;
      await field.getByRole("combobox").first().click({ timeout: FILL_TIMEOUT });
      await page.getByRole("option", { name: labelRegex }).first().click({ timeout: FILL_TIMEOUT });
      return true;
    } catch (_error) {
      // Try next label.
    }
  }
  return false;
}

async function fillGreenhouseQuestionByLabel(page, labelPattern, value) {
  if (!value) return false;
  const questions = await greenhouseQuestions(page);
  const question = questions.find((item) => labelPattern.test(item.label || item.name || ""));
  if (!question) return false;
  for (const field of question.fields || []) {
    if (!field.name || !["input_text", "textarea"].includes(field.type)) continue;
    const target = page.locator(`[name="${cssEscape(field.name)}"], [id="${cssEscape(field.name)}"]`).first();
    try {
      if (await target.count()) {
        await target.fill(String(value), { timeout: FILL_TIMEOUT });
        return true;
      }
    } catch (_error) {
      // Try next field.
    }
  }
  return false;
}

async function greenhouseQuestions(page) {
  if (page.__greenhouseQuestions) return page.__greenhouseQuestions;
  page.__greenhouseQuestions = await page.evaluate(() => {
    const remix = window.__remixContext;
    const loaderData = remix && remix.state && remix.state.loaderData;
    if (!loaderData) return [];
    const route = Object.values(loaderData).find((value) => value && value.jobPost);
    const jobPost = route && route.jobPost;
    const questions = Array.isArray(jobPost && jobPost.questions) ? jobPost.questions : [];
    const demographic = jobPost && jobPost.demographic_questions && Array.isArray(jobPost.demographic_questions.questions)
      ? jobPost.demographic_questions.questions.map((question) => ({
          label: question.name,
          fields: [
            {
              name: String(question.id),
              type: "multi_value_single_select",
              values: (question.answer_options || []).map((option) => ({ label: option.name, value: option.id })),
            },
          ],
        }))
      : [];
    return [...questions, ...demographic];
  }).catch(() => []);
  return page.__greenhouseQuestions;
}

async function answerRelocationQuestions(page, app, preferences, defaults, actionItems) {
  const allowedStates = new Set((preferences.relocation_allowed_states || []).map((item) => String(item).toUpperCase()));
  const jobText = `${app.location || ""} ${app.role || ""} ${app.url || ""}`;
  const jobMentionsAllowedState = [...allowedStates].some((state) => new RegExp(`\\b${escapeRegExp(state)}\\b`, "i").test(jobText));
  const jobMentionsAllowedName = /(california|san francisco|san jose|palo alto|mountain view|sunnyvale|los angeles|washington|seattle|bellevue)/i.test(jobText);
  const jobMentionsOtherState = /\b(NY|New York|Texas|TX|Colorado|CO|Massachusetts|MA|Illinois|IL|Florida|FL|London|United Kingdom|Singapore|France|Japan|Korea|Australia|Qatar|Dubai|Abu Dhabi)\b/i.test(jobText);
  const canRelocate = Boolean(jobMentionsAllowedState || jobMentionsAllowedName || (!jobMentionsOtherState && preferences.willing_to_relocate));

  await clickChoiceByLabel(page, /open to working.*onsite|central offices|office/i, [
    ...(defaults.office_preference_order || []),
    ...(preferences.preferred_locations_order || []),
    defaults.office_preference,
  ]);
  await clickChoiceByLabel(page, /willing to relocate|relocat/i, [canRelocate ? "Yes" : "No"]);
  await selectGreenhouseQuestionByLabel(page, /open to working.*onsite|central offices|office/i, [
    ...(defaults.office_preference_order || []),
    ...(preferences.preferred_locations_order || []),
    defaults.office_preference,
  ]);
  await selectGreenhouseQuestionByLabel(page, /willing to relocate|relocat/i, [canRelocate ? "Yes" : "No"]);
  if (!canRelocate) {
    actionItems.add("Relocation question answered conservatively as No because the role did not appear to be in CA or WA.");
  }
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function cssEscape(value) {
  return String(value).replace(/["\\]/g, "\\$&");
}

function keyboardFor(surface) {
  return surface.keyboard || surface.page().keyboard;
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
  const chromePath = args.chrome || process.env.JOB_SEARCH_CHROME_PATH || DEFAULT_CHROME_PATH;
  const launchOptions = {
    headless: false,
    viewport: { width: 1280, height: 1400 },
  };
  if (chromePath && fs.existsSync(chromePath)) {
    launchOptions.executablePath = chromePath;
    console.log(`Using Chrome: ${chromePath}`);
  }
  const context = await chromium.launchPersistentContext(userDataDir, {
    ...launchOptions,
  });
  const page = context.pages()[0] || await context.newPage();

  const outputDir = path.join(selected.dir, "output", slug(app.company), slug(app.role));
  fs.mkdirSync(outputDir, { recursive: true });
  const screenshotPath = path.join(outputDir, "pre_submit.png");
  const actionItems = new Set(app.action_items || []);

  try {
    await page.goto(app.url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(2500);
    const applicationSurface = await openApplicationSurface(page);
    console.log(`Using ${applicationSurface === page ? "main page" : "embedded application frame"} for form filling.`);
    const visibleText = await combinedVisibleText(page, applicationSurface);

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
    console.log("Filling contact fields...");
    await fillFirst(applicationSurface, ['input[name="first_name"]', 'input[id="first_name"]'], firstName(personal.legal_name || personal.name));
    await fillFirst(applicationSurface, ['input[name*="last" i]', 'input[aria-label*="last" i]', 'input[id*="last" i]'], lastName(personal.name));
    await fillFirst(applicationSurface, ['input[name*="preferred" i]', 'input[aria-label*="preferred" i]', 'input[id*="preferred" i]'], firstName(personal.name));
    await fillFirst(applicationSurface, ['input[type="email"]', 'input[name*="email" i]', 'input[aria-label*="email" i]'], personal.email);
    await fillFirst(applicationSurface, ['input[type="tel"]', 'input[name*="phone" i]', 'input[aria-label*="phone" i]'], personal.phone);
    await fillFirst(applicationSurface, ['input[name*="location" i]', 'input[aria-label*="location" i]', 'input[id*="location" i]'], personal.location);
    await fillFirst(applicationSurface, ['input[name*="linkedin" i]', 'input[aria-label*="linkedin" i]', 'input[id*="linkedin" i]'], links.linkedin);
    await fillFirst(applicationSurface, ['input[name*="github" i]', 'input[aria-label*="github" i]', 'input[id*="github" i]', 'input[placeholder*="github" i]'], links.github);
    await fillByLabel(applicationSurface, /github/i, links.github);
    await fillFirst(applicationSurface, ['input[name*="website" i]', 'input[aria-label*="website" i]', 'input[id*="website" i]', 'input[name*="portfolio" i]', 'input[aria-label*="portfolio" i]'], links.website);
    await chooseCountry(applicationSurface, personal.country || "United States");

    console.log("Uploading resume...");
    const resumePath = resolveWorkspacePath(root, selected.dir, profile.resume_file || app.resume_path || "");
    if (resumePath && fs.existsSync(resumePath)) {
      const fileInputs = await applicationSurface.locator('input[type="file"]').all();
      if (fileInputs.length > 0) {
        await fileInputs[0].setInputFiles(resumePath);
      }
    } else {
      actionItems.add("Resume upload skipped because profile.resume_file or app.resume_path did not point to an existing file.");
    }

    console.log("Uploading cover letter and filling structured questions...");
    await uploadCoverLetter(applicationSurface, app.cover_letter_path, root);
    await fillFirst(applicationSurface, ['textarea[name*="cover" i]', 'textarea[aria-label*="cover" i]'], coverLetterText(app.cover_letter_path, root));
    await fillFirst(applicationSurface, ['input[name*="authorized" i]', 'textarea[name*="authorized" i]'], defaults.authorized_to_work);
    await fillFirst(applicationSurface, ['input[name*="sponsor" i]', 'textarea[name*="sponsor" i]'], defaults.requires_sponsorship);
    if (applicationSurface === page) {
      await fillStructuredApplicationFields(applicationSurface, profile, app, actionItems);
    } else {
      actionItems.add("Embedded application form detected; review EEO, authorization, sponsorship, and screening fields manually.");
    }

    console.log("Capturing pre-submit screenshot...");
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

async function uploadCoverLetter(page, relativePath, root) {
  if (!relativePath) return false;
  const fullPath = path.isAbsolute(relativePath) ? relativePath : path.resolve(root, "..", relativePath);
  if (!fs.existsSync(fullPath)) return false;
  const uploadPath = textUploadPath(fullPath);
  try {
    const input = page.locator('input[type="file"][id*="cover" i], input[type="file"][name*="cover" i]').first();
    if (await input.count()) {
      await input.setInputFiles(uploadPath);
      return true;
    }
  } catch (_error) {
    // Fall back to manual review.
  }
  return false;
}

async function openApplicationSurface(page) {
  for (const locator of [
    page.getByRole("button", { name: /apply now|application form/i }).first(),
    page.getByRole("link", { name: /apply now|apply/i }).first(),
    page.locator('button[aria-label*="application" i], button:has-text("Apply Now"), a:has-text("Apply Now")').first(),
  ]) {
    try {
      if (await locator.count()) {
        await locator.click({ timeout: NAVIGATION_TIMEOUT });
        await page.waitForTimeout(4000);
        break;
      }
    } catch (_error) {
      // Try the next apply control.
    }
  }

  const greenhouseFrame = page.frames().find((frame) => /greenhouse\.io\/embed|greenhouse\.io\/.*job_app/i.test(frame.url()));
  return greenhouseFrame || page;
}

async function combinedVisibleText(page, applicationSurface) {
  const blocks = [];
  try {
    blocks.push(await page.locator("body").innerText({ timeout: 10000 }));
  } catch (_error) {
    // Ignore.
  }
  if (applicationSurface !== page) {
    try {
      blocks.push(await applicationSurface.locator("body").innerText({ timeout: 10000 }));
    } catch (_error) {
      // Ignore.
    }
  }
  return blocks.join("\n").toLowerCase();
}

function textUploadPath(markdownPath) {
  const parsed = path.parse(markdownPath);
  const uploadPath = path.join(parsed.dir, `${parsed.name}.txt`);
  if (!fs.existsSync(uploadPath) || fs.statSync(uploadPath).mtimeMs < fs.statSync(markdownPath).mtimeMs) {
    fs.writeFileSync(uploadPath, fs.readFileSync(markdownPath, "utf8"), "utf8");
  }
  return uploadPath;
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
