#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

const FILL_TIMEOUT = 900;
const NAVIGATION_TIMEOUT = 5000;
const DEFAULT_FILL_RETRIES = 1;
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

function intArg(value, fallback) {
  if (value === true || value === undefined || value === null || value === "") return fallback;
  const parsed = Number.parseInt(String(value), 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
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
    "id", "company", "role", "url", "platform", "job_number", "external_job_id", "location", "status", "fit_score", "ats_score",
    "date_found", "posted_at", "updated_at", "first_seen", "last_seen", "source", "source_query",
    "freshness_source", "target_track", "matched_tracks", "resume_file", "date_applied",
    "resume_path", "cover_letter_path", "screenshot_path", "notes"
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
  return page.locator(".field-wrapper, .select, fieldset, .education--form, .ashby-application-form-field-entry, [data-field-path]").filter({ hasText: regex }).first();
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
  await fillFirst(page, ["#candidate-location"], personal.city || personal.location);
  await fillByLabel(page, /location|candidate location|city/i, personal.location);
  await fillGreenhouseQuestionByLabel(page, /candidate location|location/i, personal.location);

  await fillSchoolField(page, education, actionItems);
  await selectByLabel(page, /^degree/i, [education.degree, "Master of Science", "Master's Degree", "Masters"]);
  await selectByLabel(page, /discipline|major/i, [education.major, "Computer Science"]);

  await clickChoiceByLabel(page, /personal pronouns|pronouns/i, ["She /Her", "She/Her", "She / Her"]);
  await fillByLabel(page, /current company/i, "Youmigo Tech");
  await fillByLabel(page, /current title/i, "Full-stack Software Engineer");
  await answerDemographicQuestion(page, /how did you hear|referral source/i, [
    defaults.referral_source || "Company Website",
    "Company Website",
    "Web Search",
    "Other",
  ]);
  await fillByLabel(page, /where or from who did you hear|where.*hear/i, defaults.referral_source || "Company Website");
  await fillByLabel(page, /current visa status|visa status/i, defaults.current_visa_status || workAuth.status || "Green Card");
  await fillGreenhouseQuestionByLabel(page, /current visa status|basis of your current employment authorization/i, defaults.current_visa_status || workAuth.status || "Green Card");

  await clickChoiceByLabel(page, /authorized|legally authorized/i, ["Yes"]);
  await answerDemographicQuestion(page, /authorized.*United States|legally authorized|legally eligible/i, ["Yes"]);
  await clickChoiceByLabel(page, /h-?1b|sponsorship|sponsor/i, ["No"]);
  await selectGreenhouseQuestionByLabel(page, /sponsorship|sponsor/i, ["No"]);
  await answerDemographicQuestion(page, /visa sponsorship|sponsorship/i, ["No"]);

  await answerDemographicQuestion(page, /at least 4 years.*software engineering experience/i, ["No"]);
  await answerDemographicQuestion(page, /production applications.*TypeScript|TypeScript/i, ["Yes"]);
  await fillGreenhouseQuestionByLabel(
    page,
    /Angular.*async|remote data state|architect around it/i,
    "I have not used Angular as my primary production framework, but the async-state issue I watch for is allowing loading, error, stale, and success states to spread across components. I usually architect around that by keeping remote data behind a clear service/store boundary, modeling request state explicitly, cancelling or ignoring stale responses, and keeping components focused on rendering predictable typed state.",
  );
  await fillGreenhouseQuestionByLabel(
    page,
    /experience building, deploying, managing, or scaling AI agent solutions.*production/i,
    "Yes. In Youmigo, I built production AI-assisted workflows around profile understanding, retrieval, matching, and structured generation. I worked on backend APIs, prompt and context design, data quality checks, and deployment/debugging paths so generated recommendations and user-facing outputs stayed reliable in production.",
  );
  await fillGreenhouseQuestionByLabel(
    page,
    /full-stack application leveraging Agent logic/i,
    "I built Youmigo as a full-stack application with a Swift iOS frontend, backend services, database-backed user/profile state, and AI-driven matching and generation workflows. I connected user inputs, retrieval/context logic, APIs, and UI review flows so the agent-assisted results could be inspected and improved by users.",
  );
  await answerDemographicQuestion(page, /bound by any agreements.*restrict.*work|non-compete|non-solicitation|confidentiality/i, ["No"]);

  await answerRelocationQuestions(page, app, preferences, defaults, actionItems);
  await answerDemographicQuestion(page, /hybrid work schedule|work.*office/i, ["Yes"]);
  await answerDemographicQuestion(page, /which .*office|office.*interested/i, [
    defaults.office_preference || "",
    "San Francisco",
    "SF",
    "New York",
  ]);
  await answerDemographicQuestion(page, /require.*relocation|relocation.*work/i, ["Yes"]);
  await answerDemographicQuestion(page, /new hire onboarding|first week/i, ["Yes"]);
  await answerDemographicQuestion(page, /salary.*estimated range|compensation.*range/i, ["Yes"]);
  await clickChoiceByLabel(page, /acknowledge|confirm|agree/i, ["Yes, I acknowledge, agree, and confirm.", "Yes"]);
  await selectGreenhouseQuestionByLabel(page, /acknowledge|confirm|agree/i, ["Yes, I acknowledge, agree, and confirm.", "Yes"]);

  const demographicResults = [
    await answerDemographicQuestion(page, /gender/i, [defaults.gender || "Female", "Female"]),
    await answerDemographicQuestion(page, /hispanic|latino/i, [defaults.hispanic_latino === "No" ? "No" : defaults.hispanic_latino, "No"]),
    await answerDemographicQuestion(page, /race|ethnicity/i, [defaults.race_ethnicity || "Asian", "Asian"]),
    await answerDemographicQuestion(page, /veteran/i, [defaults.veteran_status === "No" ? "I am not a protected veteran" : defaults.veteran_status, "No", "I am not a protected veteran"]),
    await answerDemographicQuestion(page, /disability/i, [defaults.disability_status === "No" ? "No, I don't have a disability and have not had one in the past" : defaults.disability_status, "No"]),
    await answerDemographicQuestion(page, /sexual orientation/i, [defaults.sexual_orientation || "Heterosexual", "Heterosexual"]),
  ];
  if (!demographicResults.every(Boolean)) {
    actionItems.add("Review EEO demographic fields manually: gender, Hispanic/Latino, race, veteran, disability, and sexual orientation.");
  }
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

async function fillAshbyCommonFields(page, profile, app, actionItems) {
  const defaults = profile.application_defaults || {};
  const preferences = profile.preferences || {};
  const canCommuteOrRelocate = canWorkAtRoleLocation(app, preferences);

  const result = await page.evaluate(
    ({ defaults, canCommuteOrRelocate }) => {
      const actions = [];
      const visible = (node) => {
        if (!node || !(node instanceof HTMLElement)) return false;
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
      };
      const normalize = (text) => String(text || "").replace(/\s+/g, " ").trim();
      const escape = (text) => String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const blocks = () => Array.from(document.querySelectorAll("fieldset, section, form > div, main div, div"))
        .filter((node) => visible(node) && normalize(node.innerText).length)
        .sort((a, b) => normalize(a.innerText).length - normalize(b.innerText).length);
      const findBlock = (pattern, selector) => blocks().find((node) => {
        if (!pattern.test(normalize(node.innerText))) return false;
        if (!selector) return true;
        return Array.from(node.querySelectorAll(selector)).some(visible);
      });
      const controlText = (node) => normalize([
        node.innerText,
        node.textContent,
        node.getAttribute("aria-label"),
        node.getAttribute("value"),
      ].filter(Boolean).join(" "));
      const clickControl = (control) => {
        if (!control) return false;
        control.scrollIntoView({ block: "center", inline: "nearest" });
        if (control.tagName === "INPUT" && ["radio", "checkbox"].includes(control.type)) {
          if (control.checked) return true;
          control.checked = true;
          control.dispatchEvent(new Event("input", { bubbles: true }));
          control.dispatchEvent(new Event("change", { bubbles: true }));
          return true;
        }
        if (String(control.className || "").includes("active") || control.getAttribute("aria-pressed") === "true") {
          return true;
        }
        control.click();
        return true;
      };
      const clickChoice = (questionPattern, choices) => {
        const block = findBlock(questionPattern, "button, label, input[type='radio'], input[type='checkbox'], [role='radio'], [role='checkbox']");
        if (!block) return false;
        const controls = Array.from(block.querySelectorAll("button, label, input[type='radio'], input[type='checkbox'], [role='radio'], [role='checkbox']"))
          .filter(visible);
        for (const choice of choices.filter(Boolean)) {
          const exact = new RegExp(`^${escape(choice)}$`, "i");
          const loose = new RegExp(escape(choice), "i");
          const control = controls.find((node) => exact.test(controlText(node))) || controls.find((node) => loose.test(controlText(node)));
          if (clickControl(control)) {
            actions.push(`answered:${normalize(choice)}`);
            return true;
          }
        }
        return false;
      };
      const fillText = (questionPattern, value) => {
        const block = findBlock(questionPattern, "input:not([type='hidden']):not([type='file']), textarea");
        if (!block || !value) return false;
        const input = Array.from(block.querySelectorAll("input:not([type='hidden']):not([type='file']), textarea"))
          .filter(visible)
          .find((node) => !node.value);
        if (!input) return false;
        input.scrollIntoView({ block: "center", inline: "nearest" });
        input.focus();
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        actions.push(`filled:${normalize(value)}`);
        return true;
      };

      fillText(/where have you most recently worked/i, "Youmigo Tech");
      clickChoice(/require company sponsorship/i, [defaults.requires_sponsorship || "No", "No"]);
      clickChoice(/commuting distance|open to relocation|local snowflake office/i, [canCommuteOrRelocate ? "YES" : "NO", canCommuteOrRelocate ? "Yes" : "No"]);
      clickChoice(/worked at snowflake in the past/i, ["NO", "No"]);
      clickChoice(/authorized to work in the country/i, [defaults.authorized_to_work || "YES", "YES", "Yes"]);
      clickChoice(/U\.S\. person.*best describes|describes your.*U\.S\. person/i, ["I am a U.S. person"]);
      clickChoice(/PricewaterhouseCoopers|PwC/i, ["No - I have never been employed by PwC"]);
      clickChoice(/government or military entity|government contractor/i, ["NO", "No"]);
      clickChoice(/redact or remove age-identifying|school attendance or graduation/i, ["I Acknowledge", "Acknowledge"]);
      clickChoice(/Candidate Privacy Notice/i, ["I have read and agree to the Snowflake Candidate Privacy Notice", "I have read"]);
      clickChoice(/Gender|Input gender/i, [defaults.gender || "Female", "Female"]);
      clickChoice(/Race|Hispanic or Latino/i, [defaults.race_ethnicity || defaults.race || "Asian (Not Hispanic or Latino)", "Asian"]);
      clickChoice(/Veteran Status|protected veteran/i, [defaults.veteran_status === "No" ? "I am not a protected veteran" : defaults.veteran_status, "I am not a protected veteran"]);
      clickChoice(/Disability Status/i, [defaults.disability_status === "No" ? "No, I don't have a disability and have not had one in the past" : defaults.disability_status, "No, I don't have a disability"]);

      return actions;
    },
    { defaults, canCommuteOrRelocate },
  ).catch(() => []);

  if (!result.length) {
    actionItems.add("Ashby common screening fields may need manual review.");
  }

  await fillAshbyTextByQuestion(page, /where have you most recently worked/i, "Youmigo Tech");
  await clickAshbyButtonByQuestion(page, /require company sponsorship/i, "No");
  await clickAshbyButtonByQuestion(page, /commuting distance|open to relocation|local snowflake office/i, canCommuteOrRelocate ? "Yes" : "No");
  await clickAshbyButtonByQuestion(page, /worked at Snowflake in the past/i, "No");
  await clickAshbyButtonByQuestion(page, /authorized to work in the country/i, "Yes");
  await clickAshbyChoiceByQuestion(page, /U\.S\. person.*best describes|describes your.*U\.S\. person/i, "I am a U.S. person");
  await clickAshbyChoiceByQuestion(page, /PricewaterhouseCoopers|PwC/i, "No - I have never been employed by PwC");
  await clickAshbyButtonByQuestion(page, /government or military entity|government contractor/i, "No");
  await clickAshbyChoiceByQuestion(page, /redact or remove age-identifying|school attendance or graduation/i, "I Acknowledge");
  await clickAshbyChoiceByQuestion(page, /Candidate Privacy Notice/i, "I have read and agree to the Snowflake Candidate Privacy Notice");
  await clickAshbyChoiceByQuestion(page, /Gender|Input gender/i, defaults.gender || "Female");
  await clickAshbyChoiceByQuestion(page, /Race|Hispanic or Latino/i, defaults.race_ethnicity || "Asian");
  await clickAshbyChoiceByQuestion(page, /Veteran Status|protected veteran/i, defaults.veteran_status === "No" ? "I am not a protected veteran" : defaults.veteran_status);
  await clickAshbyChoiceByQuestion(page, /Disability Status/i, defaults.disability_status === "No" ? "No, I don't have a disability and have not had one in the past" : defaults.disability_status);

  await clickAshbyDataPathButton(page, "c773adde-72b8-494f-8f4f-bb9b84608c29", "No");
  await fillAshbyDataPathText(page, "1c1690a4-cce6-4e38-99cb-71dc879c5164", profile.personal?.phone || "");
  await clickAshbyDataPathButton(page, "4c8e248b-f134-416f-9fa5-38c9e679f7b1", canCommuteOrRelocate ? "Yes" : "No");
  await clickAshbyDataPathButton(page, "a3cc08ef-4552-444a-b493-c608c65670df", "Yes");
  await clickAshbyDataPathButton(page, "962f2552-c3ca-4d6c-a7d8-b7b1340558ae", "No");
  await clickAshbyDataPathButton(page, "a7d1a4bb-e59b-4149-a733-f46f352b0cb5", "No");
  await clickAshbyDataPathChoice(page, "_systemfield_eeoc_race", defaults.race_ethnicity || "Asian");
}

async function ashbyField(page, questionPattern) {
  return page.locator(".ashby-application-form-field-entry, fieldset, [data-field-path]").filter({ hasText: questionPattern }).first();
}

async function fillAshbyTextByQuestion(page, questionPattern, value) {
  if (!value) return false;
  try {
    const field = await ashbyField(page, questionPattern);
    const input = field.locator("input:not([type='hidden']):not([type='file']), textarea").first();
    if (!(await input.count())) return false;
    await input.fill(String(value), { timeout: FILL_TIMEOUT });
    return true;
  } catch (_error) {
    return false;
  }
}

async function clickAshbyButtonByQuestion(page, questionPattern, label) {
  try {
    const field = await ashbyField(page, questionPattern);
    const button = field.getByRole("button", { name: new RegExp(`^${escapeRegExp(label)}$`, "i") }).first();
    if (!(await button.count())) return false;
    const isActive = await button.evaluate((node) => String(node.className || "").includes("active") || node.getAttribute("aria-pressed") === "true").catch(() => false);
    if (!isActive) {
      await button.click({ timeout: FILL_TIMEOUT });
    }
    return true;
  } catch (_error) {
    return false;
  }
}

async function clickAshbyChoiceByQuestion(page, questionPattern, label) {
  try {
    const field = await ashbyField(page, questionPattern);
    const exact = new RegExp(`^${escapeRegExp(label)}$`, "i");
    const radio = field.getByRole("radio", { name: exact }).first();
    if (await radio.count()) {
      await radio.check({ timeout: FILL_TIMEOUT });
      return true;
    }
    const checkbox = field.getByRole("checkbox", { name: exact }).first();
    if (await checkbox.count()) {
      await checkbox.check({ timeout: FILL_TIMEOUT });
      return true;
    }
    await field.getByText(exact).first().click({ timeout: FILL_TIMEOUT });
    return true;
  } catch (_error) {
    return false;
  }
}

async function clickAshbyDataPathButton(page, dataPath, label) {
  try {
    const field = page.locator(`[data-field-path="${cssEscape(dataPath)}"]`).first();
    if (!(await field.count())) return false;
    const button = field.getByRole("button", { name: new RegExp(`^${escapeRegExp(label)}$`, "i") }).first();
    if (!(await button.count())) return false;
    const isActive = await button.evaluate((node) => String(node.className || "").includes("active")).catch(() => false);
    if (!isActive) {
      await button.click({ timeout: FILL_TIMEOUT });
    }
    return true;
  } catch (_error) {
    return false;
  }
}

async function fillAshbyDataPathText(page, dataPath, value) {
  if (!value) return false;
  try {
    const field = page.locator(`[data-field-path="${cssEscape(dataPath)}"]`).first();
    if (!(await field.count())) return false;
    const input = field.locator("input:not([type='hidden']):not([type='file']), textarea").first();
    if (!(await input.count())) return false;
    await input.fill(String(value), { timeout: FILL_TIMEOUT });
    return true;
  } catch (_error) {
    return false;
  }
}

async function clickAshbyDataPathChoice(page, dataPath, label) {
  try {
    const field = page.locator(`[data-field-path="${cssEscape(dataPath)}"]`).first();
    if (!(await field.count())) return false;
    const exact = new RegExp(`^${escapeRegExp(label)}(?: \\(.*\\))?$`, "i");
    const radio = field.getByRole("radio", { name: exact }).first();
    if (await radio.count()) {
      await radio.check({ timeout: FILL_TIMEOUT });
      return true;
    }
    await field.getByText(exact).first().click({ timeout: FILL_TIMEOUT });
    return true;
  } catch (_error) {
    return false;
  }
}

function canWorkAtRoleLocation(app, preferences) {
  const allowedStates = new Set((preferences.relocation_allowed_states || []).map((item) => String(item).toUpperCase()));
  const jobText = `${app.location || ""} ${app.role || ""} ${app.url || ""}`;
  const mentionsAllowedState = [...allowedStates].some((state) => new RegExp(`\\b${escapeRegExp(state)}\\b`, "i").test(jobText));
  const mentionsAllowedName = /(california|san francisco|san jose|palo alto|mountain view|sunnyvale|los angeles|washington|seattle|bellevue|menlo park|bay area)/i.test(jobText);
  const mentionsOtherState = /\b(NY|New York|Texas|TX|Colorado|CO|Massachusetts|MA|Illinois|IL|Florida|FL|London|United Kingdom|Singapore|France|Japan|Korea|Australia|Qatar|Dubai|Abu Dhabi)\b/i.test(jobText);
  return Boolean(mentionsAllowedState || mentionsAllowedName || (!mentionsOtherState && preferences.willing_to_relocate));
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
    viewport: null,
    args: [
      "--start-maximized",
      "--window-size=1440,1000",
      "--new-window",
      "--disable-extensions",
      "--disable-component-extensions-with-background-pages",
      "--disable-features=Translate",
    ],
  };
  if (args["allow-extensions"]) {
    launchOptions.args = [];
  }
  if (chromePath && fs.existsSync(chromePath)) {
    launchOptions.executablePath = chromePath;
    console.log(`Using Chrome: ${chromePath}`);
  }
  const context = await chromium.launchPersistentContext(userDataDir, {
    ...launchOptions,
  });
  const page = context.pages()[0] || await context.newPage();
  await page.bringToFront().catch(() => {});

  const outputDir = path.join(selected.dir, "output", slug(app.company), slug(app.role));
  fs.mkdirSync(outputDir, { recursive: true });
  const screenshotPath = path.join(outputDir, "pre_submit.png");
  const actionItems = new Set(app.action_items || []);

  try {
    await page.goto(app.url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.bringToFront().catch(() => {});
    await page.waitForTimeout(2500);
    const applicationSurface = await openApplicationSurface(page);
    console.log(`Using ${applicationSurface === page ? "main page" : "embedded application frame"} for form filling.`);
    const visibleText = await combinedVisibleText(page, applicationSurface);

    const hasApplicationForm = await applicationSurface.locator('input, textarea, select, button').count().catch(() => 0) > 0
      && /first name|last name|email|resume|cover letter|apply for this job/i.test(visibleText);
    if (/captcha|verify you are human|e-signature|signature/.test(visibleText) || (!hasApplicationForm && /sign in|log in|create account/.test(visibleText))) {
      actionItems.add("Browser stopped for login/CAPTCHA/account/signature step.");
      await page.screenshot({ path: screenshotPath, fullPage: true });
      updateApp(tracker, app.id, { status: "needs_review", screenshot_path: screenshotPath, action_items: [...actionItems] });
      writeJson(trackerPath, tracker);
      writeCsv(selected.dir, tracker);
      console.log(`Stopped for manual review. Screenshot: ${screenshotPath}`);
      return;
    }

    if (isMicrosoftJob(app)) {
      const validation = validateMicrosoftPage(visibleText, app);
      if (!validation.ok) {
        actionItems.add(validation.message);
        actionItems.add("Microsoft Careers portal was not autofilled because the visible role/job number did not match the target application.");
        await page.screenshot({ path: screenshotPath, fullPage: true });
        updateApp(tracker, app.id, {
          status: "needs_review",
          portal_mode: "manual",
          screenshot_path: screenshotPath,
          action_items: [...actionItems],
        });
        writeJson(trackerPath, tracker);
        writeCsv(selected.dir, tracker);
        console.log(`Stopped before filling Microsoft portal: ${validation.message}`);
        console.log(`Screenshot: ${screenshotPath}`);
        return;
      }
      actionItems.add("Microsoft Careers portal matched the target role, but final job identity and submit still require manual review.");
    }

    const maxRetries = intArg(args["fill-retries"], DEFAULT_FILL_RETRIES);
    let requiredIssues = [];
    for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
      if (attempt > 0) {
        console.log(`Retrying incomplete fields (${attempt}/${maxRetries})...`);
      }
      await fillApplicationOnce(applicationSurface, page, root, selected, profile, app, actionItems);
      await scrollToSubmitOrBottom(page, applicationSurface);
      requiredIssues = await requiredIssueSummary(applicationSurface);
      if (!requiredIssues.length) break;
    }
    if (requiredIssues.length) {
      actionItems.add(`Autofill stopped after ${maxRetries + 1} attempt(s). Manually review required fields: ${requiredIssues.join("; ")}.`);
      console.log(`Autofill stopped with required fields still flagged: ${requiredIssues.join("; ")}`);
    }

    await scrollToSubmitOrBottom(page, applicationSurface);
    console.log("Capturing pre-submit screenshot...");
    actionItems.add("Review all fields manually. Final application submit must be clicked by you, not automation.");
    const fullPageScreenshot = Boolean(args["full-screenshot"]) || /greenhouse|lever/i.test(app.platform || "");
    await page.screenshot({ path: screenshotPath, fullPage: fullPageScreenshot });
    updateApp(tracker, app.id, { status: "needs_review", screenshot_path: screenshotPath, action_items: [...actionItems] });
    writeJson(trackerPath, tracker);
    writeCsv(selected.dir, tracker);
    console.log(`Filled conservative fields and stopped before submit. Screenshot: ${screenshotPath}`);
  } finally {
    if (args["close-when-done"]) {
      console.log("Browser closed because --close-when-done was passed.");
      await context.close();
    } else {
      console.log("Browser is staying open for your manual review. Press Ctrl+C in this terminal after you finish.");
      await new Promise(() => {});
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

function isMicrosoftJob(app) {
  return String(app.platform || "").toLowerCase() === "microsoft_jobs"
    || /jobs\.careers\.microsoft\.com|apply\.careers\.microsoft\.com/i.test(String(app.url || ""));
}

function validateMicrosoftPage(visibleText, app) {
  const text = normalizeForMatch(visibleText);
  const role = normalizeForMatch(app.role || "");
  const roleTokens = role.split(" ").filter((token) => token.length > 1);
  const roleMatched = role && text.includes(role);
  const tokenMatched = roleTokens.length >= 3 && roleTokens.every((token) => text.includes(token));
  const jobNumber = normalizeForMatch(app.job_number || "");
  const externalJobId = normalizeForMatch(app.external_job_id || microsoftIdFromUrl(app.url));
  const jobNumberMatched = Boolean(jobNumber && text.includes(jobNumber));
  const externalJobIdMatched = Boolean(externalJobId && text.includes(externalJobId));

  if (roleMatched || tokenMatched || jobNumberMatched || externalJobIdMatched) {
    return { ok: true, message: "Microsoft portal matches target role or job number." };
  }

  const expected = [
    app.role ? `role "${app.role}"` : "",
    app.job_number ? `job number ${app.job_number}` : "",
    app.external_job_id ? `external id ${app.external_job_id}` : "",
  ].filter(Boolean).join(" / ");
  return {
    ok: false,
    message: `Expected Microsoft ${expected || "target job"}, but the current portal page did not show it.`,
  };
}

function microsoftIdFromUrl(url) {
  const match = String(url || "").match(/\/job\/(\d+)/i);
  return match ? match[1] : "";
}

function normalizeForMatch(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
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
    const ashbyAdditionalAttachment = page.locator('[data-field-path="b1890667-a914-4163-b3c1-c5b694fd412c"] input[type="file"]').first();
    if (await ashbyAdditionalAttachment.count()) {
      await ashbyAdditionalAttachment.setInputFiles(uploadPath);
      return true;
    }
    const fileInputs = await page.locator('input[type="file"]').all();
    if (fileInputs.length > 1) {
      await fileInputs[fileInputs.length - 1].setInputFiles(uploadPath);
      return true;
    }
  } catch (_error) {
    // Fall back to manual review.
  }
  return false;
}

async function fillApplicationOnce(applicationSurface, page, root, selected, profile, app, actionItems) {
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
  const resumePath = resolveWorkspacePath(root, selected.dir, app.resume_file || profile.resume_file || app.resume_path || "");
  if (resumePath && fs.existsSync(resumePath)) {
    const fileInputs = await applicationSurface.locator('input[type="file"]').all();
    if (fileInputs.length > 0) {
      await fileInputs[0].setInputFiles(resumePath);
    }
  } else {
    actionItems.add("Resume upload skipped because app.resume_file, profile.resume_file, or app.resume_path did not point to an existing file.");
  }

  console.log("Uploading cover letter and filling structured questions...");
  const coverLetter = coverLetterText(app.cover_letter_path, root);
  const coverLetterUploaded = await uploadCoverLetter(applicationSurface, app.cover_letter_path, root);
  await fillFirst(applicationSurface, ['input[type="tel"]', 'input[name*="phone" i]', 'input[aria-label*="phone" i]'], personal.phone);
  const coverLetterFilled = await fillCoverLetterFields(applicationSurface, coverLetter);
  if (coverLetter && !coverLetterFilled && !coverLetterUploaded) {
    actionItems.add("Cover letter was generated but no compatible cover letter field or upload control was confirmed.");
  }
  await fillFirst(applicationSurface, ['input[name*="authorized" i]', 'textarea[name*="authorized" i]'], defaults.authorized_to_work);
  await fillFirst(applicationSurface, ['input[name*="sponsor" i]', 'textarea[name*="sponsor" i]'], defaults.requires_sponsorship);
  if (applicationSurface === page) {
    await fillStructuredApplicationFields(applicationSurface, profile, app, actionItems);
    await fillAshbyCommonFields(applicationSurface, profile, app, actionItems);
  } else {
    actionItems.add("Embedded application form detected; review EEO, authorization, sponsorship, and screening fields manually.");
  }
  await fillFirst(applicationSurface, ['input[type="tel"]', 'input[name*="phone" i]', 'input[aria-label*="phone" i]'], personal.phone);
  await fillAshbyDataPathText(applicationSurface, "1c1690a4-cce6-4e38-99cb-71dc879c5164", personal.phone);
}

async function requiredIssueSummary(page) {
  try {
    return await page.evaluate(() => {
      const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
      const issues = new Set();
      const errorNodes = [...document.querySelectorAll("body *")].filter((node) => {
        const text = clean(node.textContent);
        return /^This field is required\.?$/i.test(text) || /^Please enter your location$/i.test(text);
      });
      for (const node of errorNodes) {
        const container = node.closest("fieldset, .field, [data-qa], div") || node.parentElement;
        const label = container?.querySelector("label, legend") || node.closest("div")?.querySelector("label, legend");
        const labelText = clean(label?.textContent).replace(/\*$/, "").trim();
        issues.add(labelText || clean(node.textContent));
      }
      const invalidFields = [...document.querySelectorAll('[aria-invalid="true"], input:invalid, textarea:invalid, select:invalid')];
      for (const field of invalidFields) {
        if (field.type === "hidden" || field.offsetParent === null) continue;
        const id = field.id ? CSS.escape(field.id) : "";
        const label = id ? document.querySelector(`label[for="${id}"]`) : null;
        const labelText = clean(label?.textContent || field.getAttribute("aria-label") || field.name || field.id);
        if (labelText) issues.add(labelText.replace(/\*$/, "").trim());
      }
      return [...issues].filter(Boolean).slice(0, 10);
    });
  } catch (_error) {
    return [];
  }
}

async function fillCoverLetterFields(page, value) {
  if (!value) return false;
  let filled = false;
  for (const selector of [
    'textarea[name*="cover" i]',
    'textarea[aria-label*="cover" i]',
    'textarea[id*="cover" i]',
    'textarea[placeholder*="cover" i]',
  ]) {
    filled = (await fillFirst(page, [selector], value)) || filled;
  }
  for (const label of [/cover\s*letter/i, /letter of interest/i, /message to (the )?(hiring|recruiting) team/i]) {
    try {
      const field = fieldByLabel(page, label);
      const textarea = field.locator("textarea").first();
      if (await textarea.count()) {
        const current = await textarea.inputValue({ timeout: FILL_TIMEOUT }).catch(() => "");
        if (!current.trim()) {
          await textarea.fill(value, { timeout: FILL_TIMEOUT });
        }
        filled = true;
      }
    } catch (_error) {
      // Try the next cover-letter-shaped field.
    }
  }
  return filled;
}

async function scrollToSubmitOrBottom(page, applicationSurface) {
  for (const surface of [applicationSurface, page]) {
    try {
      const submit = surface.getByRole("button", { name: /submit|send application|finish application/i }).last();
      if (await submit.count()) {
        await submit.scrollIntoViewIfNeeded({ timeout: FILL_TIMEOUT });
        await page.waitForTimeout(500);
        return true;
      }
    } catch (_error) {
      // Fall back to a page-level scroll.
    }
  }
  try {
    const moved = await page.evaluate(() => {
      const before = window.scrollY;
      window.scrollBy({ top: window.innerHeight * 0.75, behavior: "instant" });
      return window.scrollY !== before;
    });
    await page.waitForTimeout(500);
    return moved;
  } catch (_error) {
    return false;
  }
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
  const uploadPath = path.join(parsed.dir, `${parsed.name}.rtf`);
  if (!fs.existsSync(uploadPath) || fs.statSync(uploadPath).mtimeMs < fs.statSync(markdownPath).mtimeMs) {
    fs.writeFileSync(uploadPath, markdownToRtf(fs.readFileSync(markdownPath, "utf8")), "utf8");
  }
  return uploadPath;
}

function markdownToRtf(markdown) {
  const text = String(markdown || "")
    .replace(/^#+\s*/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1");
  const escaped = text
    .replace(/[\\{}]/g, "\\$&")
    .replace(/\n/g, "\\par\n");
  return `{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\\fs22\n${escaped}\n}`;
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
