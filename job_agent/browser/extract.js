// Generic application-form extractor.
//
// Runs inside the page (page.evaluate). Finds every fillable control, works
// out the question it belongs to (label, description, required flag, options)
// and tags the element - or, for grouped controls, the question container -
// with data-ja-key so the filler can find it again. Keys come from stable
// attributes (name, id, label text), so a reload yields the same keys.
//
// Tested against Greenhouse (job-boards), Lever and Ashby application forms.
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const textOf = (el) => (el ? clean(el.innerText || el.textContent || "") : "");
  const REQ_MARK = /[*✱]/;
  const stripMarks = (s) => clean((s || "").replace(/[*✱]+/g, " ").replace(/\(required\)/gi, " "));
  const isVisible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
  const unstableId = (id) =>
    !id ||
    /^:r[0-9a-z]+:$/i.test(id) ||
    /^react-select-\d+/.test(id) ||
    /^\d+$/.test(id) ||
    /^(mui|headlessui)-/i.test(id) ||
    new RegExp(`^${UUID}_`, "i").test(id); // Ashby: <random per render>_<field id>
  // Ashby wraps every question in an element whose data-field-path is stable.
  const fieldPath = (el) => {
    const holder = el.closest("[data-field-path]");
    return holder ? holder.getAttribute("data-field-path") : null;
  };
  const slug = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 60);

  // Text of a label element without the text of any control inside it
  // (Lever wraps whole <select>s in <label>s).
  function labelText(label) {
    if (!label) return "";
    const copy = label.cloneNode(true);
    copy.querySelectorAll("select, option, textarea, input, button, svg").forEach((n) => n.remove());
    return textOf(copy);
  }

  const CONTAINER_SEL = [
    "fieldset",
    "[data-field-path]",
    ".ashby-application-form-field-entry",
    ".application-question",
    ".field-wrapper",
    "[class*='fieldEntry' i]",
    "[class*='question' i]:not(label):not(span)",
    ".form-group",
  ].join(", ");
  const questionContainer = (el) => el.closest(CONTAINER_SEL);

  const TITLE_SEL =
    "legend, .ashby-application-form-question-title, .application-label, [class*='question-title' i], " +
    "[class*='questionTitle' i], label, [class*='label' i]";

  // The question text for a container: its title element that is not the
  // label of one particular option.
  function containerTitle(container, el) {
    if (!container) return "";
    for (const c of container.querySelectorAll(TITLE_SEL)) {
      if (el && c.contains(el)) continue;
      const forId = c.getAttribute("for");
      if (forId) {
        const target = document.getElementById(forId);
        if (target && target.type && /radio|checkbox/.test(target.type) && target.closest(CONTAINER_SEL) === container) {
          // Label of a single option inside a group - skip unless it is the only control.
          const n = container.querySelectorAll("input[type=radio], input[type=checkbox]").length;
          if (n > 1) continue;
        }
      }
      if (c.querySelector("input[type=radio], input[type=checkbox]")) continue;
      const t = labelText(c);
      if (t && t.length < 600) return t;
    }
    return "";
  }

  function description(container, el) {
    const parts = [];
    const ids = (el && el.getAttribute("aria-describedby")) || "";
    for (const id of ids.split(/\s+/)) {
      if (!id || /error|placeholder/i.test(id)) continue;
      const t = textOf(document.getElementById(id));
      if (t) parts.push(t);
    }
    if (!parts.length && container) {
      for (const d of container.querySelectorAll("[class*='description' i], [class*='help' i], [class*='hint' i]")) {
        const t = textOf(d);
        if (t && !parts.includes(t)) parts.push(t);
      }
    }
    return parts.join(" ").slice(0, 700);
  }

  function labelFor(el, container) {
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb
        .split(/\s+/)
        .map((id) => labelText(document.getElementById(id)))
        .filter(Boolean)
        .join(" ");
      if (t) return t;
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      const t = labelText(l);
      if (t) return t;
    }
    const wrap = labelText(el.closest("label"));
    if (wrap) return wrap;
    const al = clean(el.getAttribute("aria-label"));
    if (al) return al;
    return containerTitle(container, el) || clean(el.getAttribute("placeholder")) || "";
  }

  function requiredFlag(el, label, container) {
    if (el && (el.required || el.getAttribute("aria-required") === "true")) return true;
    if (REQ_MARK.test(label || "") || /\(required\)/i.test(label || "")) return true;
    if (container) {
      const title = container.querySelector(TITLE_SEL);
      if (title && (/required/i.test(title.className || "") || REQ_MARK.test(title.textContent || ""))) return true;
    }
    return false;
  }

  const fields = [];
  const used = new Set();
  function addField(f, tagEl) {
    let key = f.key;
    let n = 2;
    while (used.has(key)) key = `${f.key}__${n++}`;
    used.add(key);
    f.key = key;
    tagEl.setAttribute("data-ja-key", key);
    fields.push(f);
  }
  function stableKey(el, label) {
    if (el.name && !/^\d+$/.test(el.name) && !/\s/.test(el.name) && !unstableId(el.name)) return el.name;
    if (!unstableId(el.id)) return el.id;
    const fp = fieldPath(el);
    if (fp) return fp;
    return "label:" + slug(label || el.getAttribute("placeholder") || el.type || "field");
  }

  // --- Yes/No button groups (Ashby) -------------------------------------
  const buttonGroups = new Set();
  const groupCandidates = new Set();
  document
    .querySelectorAll(".ashby-application-form-input-yesno, [class*='yesno' i]")
    .forEach((c) => groupCandidates.add(c));
  document.querySelectorAll("button[aria-pressed], button[data-option]").forEach((b) => groupCandidates.add(b.parentElement));
  for (const g of groupCandidates) {
    if (!g || buttonGroups.has(g)) continue;
    const buttons = Array.from(g.querySelectorAll("button"));
    if (buttons.length < 2 || buttons.length > 8) continue;
    const opts = buttons.map(textOf);
    if (!opts.every((t) => t && t.length <= 60)) continue;
    buttonGroups.add(g);
    const container = questionContainer(g) || g.parentElement;
    const hidden = g.querySelector("input");
    const label = containerTitle(container, g);
    const key = (container && container.getAttribute("data-field-path")) || (hidden && stableKey(hidden, label)) || "label:" + slug(label);
    addField(
      {
        key,
        kind: "button_group",
        label: stripMarks(label),
        required: requiredFlag(hidden, label, container),
        options: opts,
        description: description(container, hidden),
      },
      g
    );
  }
  const inButtonGroup = (el) => Array.from(buttonGroups).some((g) => g.contains(el));

  // --- Inputs, selects, textareas ----------------------------------------
  const seenGroups = new Set();
  for (const el of document.querySelectorAll("input, select, textarea")) {
    const type = (el.getAttribute("type") || el.tagName).toLowerCase();
    if (["hidden", "submit", "button", "reset", "image", "search"].includes(type)) continue;
    if (el.disabled || inButtonGroup(el)) continue;
    if (el.closest("[class*='captcha' i], [id*='captcha' i]")) continue;
    if (type !== "file" && type !== "checkbox" && type !== "radio" && !isVisible(el)) continue;
    const container = questionContainer(el);
    // react-select keeps a hidden, unfocusable <input required> next to the combobox for validation.
    if (
      el.getAttribute("tabindex") === "-1" &&
      el.getAttribute("role") !== "combobox" &&
      container &&
      container.querySelector("[role=combobox]")
    )
      continue;

    if (type === "radio" || type === "checkbox") {
      // Group options by question container (Ashby gives each checkbox its own name), else by name.
      let members;
      let groupId;
      const sameContainer = container
        ? Array.from(container.querySelectorAll(`input[type="${type}"]`)).filter((m) => questionContainer(m) === container)
        : [];
      if (sameContainer.length > 1) {
        members = sameContainer;
        groupId = sameContainer[0];
      } else if (el.name) {
        members = Array.from(document.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`));
        groupId = members[0];
      } else {
        members = [el];
        groupId = el;
      }
      if (seenGroups.has(groupId)) continue;
      seenGroups.add(groupId);
      const options = members.map((m) => {
        let t = "";
        if (m.id) t = labelText(document.querySelector(`label[for="${CSS.escape(m.id)}"]`));
        if (!t) t = labelText(m.closest("label"));
        if (!t) t = clean(m.getAttribute("aria-label")) || clean(m.value);
        return t;
      });
      const single = members.length === 1;
      let label = single ? labelFor(el, container) : containerTitle(container, el);
      if (!label && single) label = options[0];
      const kind = type === "radio" ? "radio" : single ? "checkbox" : "checkbox_group";
      const tag = single ? el : container && members.every((m) => container.contains(m)) ? container : el.parentElement;
      let key;
      if (fieldPath(el)) key = fieldPath(el);
      else if (members.length > 1 && el.name && members.every((m) => m.name === el.name) && !unstableId(el.name)) key = el.name;
      else if (single) key = stableKey(el, label);
      else key = "label:" + slug(label || options.join("_"));
      addField(
        {
          key,
          kind,
          label: stripMarks(label),
          required: members.some((m) => m.required) || requiredFlag(null, label, container),
          options: single && kind === "checkbox" ? [] : options,
          description: description(container, el),
        },
        tag
      );
      continue;
    }

    let label = labelFor(el, container);
    // Greenhouse labels file inputs with their button text ("Attach"); use the question title.
    if (type === "file" && /^(attach|upload|choose|browse|select|drop)\b/i.test(label)) {
      const title = containerTitle(container, el);
      if (title && title !== label) label = title;
    }
    // Unlabelled, unfocusable inputs are validation helpers of custom widgets (react-select).
    if (type !== "file" && el.getAttribute("role") !== "combobox" && (el.getAttribute("aria-hidden") === "true" ||
        (el.getAttribute("tabindex") === "-1" && !label))) continue;
    if (type === "file") {
      // Ashby's "autofill from resume" helper re-parses the resume and overwrites answers.
      const around = textOf(el.closest("div") && el.closest("div").parentElement);
      if (/autofill/i.test(label) || (!label && /autofill/i.test(around))) continue;
      if (!label && !requiredFlag(el, label, container)) continue;
    }
    let kind;
    if (el.tagName === "SELECT") kind = "select";
    else if (el.tagName === "TEXTAREA") kind = "textarea";
    else if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list") kind = "combobox";
    else if (type === "file") kind = "file";
    else kind = ["email", "tel", "url", "number", "date", "password"].includes(type) ? type : "text";

    const f = {
      key: stableKey(el, label),
      kind,
      label: stripMarks(label),
      required: requiredFlag(el, label, container),
      description: description(container, el),
      placeholder: clean(el.getAttribute("placeholder")),
    };
    if (kind === "select") {
      f.options = Array.from(el.options)
        .map((o) => clean(o.textContent))
        .filter((t) => t && !/^(select|choose|--|please select)/i.test(t));
      f.multiple = el.multiple;
    }
    if (kind === "combobox") f.multiple = /\[\]$/.test(el.name || el.id || "");
    if (kind === "file") f.accept = el.getAttribute("accept") || "";
    addField(f, el);
  }

  const submit =
    document.querySelector("button[type='submit'], input[type='submit']") ||
    Array.from(document.querySelectorAll("button")).find((b) => /submit/i.test(textOf(b)));
  if (submit) submit.setAttribute("data-ja-submit", "1");

  const pageText = textOf(document.body).slice(0, 200000);
  const captcha = {
    recaptcha: !!document.querySelector("iframe[src*='recaptcha'], .g-recaptcha, script[src*='recaptcha']"),
    hcaptcha: !!document.querySelector("iframe[src*='hcaptcha'], .h-captcha, script[src*='hcaptcha']"),
    turnstile: !!document.querySelector("iframe[src*='turnstile'], .cf-turnstile, script[src*='turnstile']"),
  };
  const aiPolicy = /\b(ai|artificial intelligence|chatgpt|llm|generative)\b[^.]{0,120}\b(policy|guideline|guidance|not (be )?(used|permitted|allowed)|prohibit)/i.test(
    pageText
  );

  return { url: location.href, title: document.title, fields, has_submit: !!submit, captcha, ai_policy_mentioned: aiPolicy };
};
