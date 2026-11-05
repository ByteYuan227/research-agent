// Generic application-form extractor.
//
// Runs inside the page (page.evaluate). Finds every fillable control, works
// out a human-readable label for it, and tags the element (or the container
// for grouped controls) with data-ja-key so the filler can find it again.
// Keys are derived from stable attributes (name, id, label text), so running
// the extractor again after a reload yields the same keys.
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const textOf = (el) => (el ? clean(el.innerText || el.textContent || "") : "");
  const isVisible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  // Unstable, framework-generated ids (React useId, react-select, etc.).
  const unstableId = (id) =>
    !id || /^:r[0-9a-z]+:$/i.test(id) || /^react-select-\d+/.test(id) || /^\d+$/.test(id) || /^(mui|headlessui)-/i.test(id);
  const slug = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 60);
  const stripRequired = (s) => clean((s || "").replace(/\*+/g, " ").replace(/\(required\)/gi, " "));

  function labelFor(el) {
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb
        .split(/\s+/)
        .map((id) => textOf(document.getElementById(id)))
        .filter(Boolean)
        .join(" ");
      if (t) return t;
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && textOf(l)) return textOf(l);
    }
    const wrap = el.closest("label");
    if (wrap && textOf(wrap)) return textOf(wrap);
    const al = el.getAttribute("aria-label");
    if (al && clean(al)) return clean(al);
    return containerLabel(el) || clean(el.getAttribute("placeholder")) || "";
  }

  // Label of the question that contains `el` (for groups and odd markup).
  function containerLabel(el) {
    const fs = el.closest("fieldset");
    if (fs) {
      const lg = fs.querySelector("legend");
      if (lg && textOf(lg)) return textOf(lg);
    }
    let p = el.parentElement;
    for (let i = 0; i < 6 && p && p !== document.body; i++, p = p.parentElement) {
      const cands = p.querySelectorAll(
        "label, legend, [class*='label' i], [class*='question' i] > div:first-child, .application-label, h3, h4"
      );
      for (const c of cands) {
        if (c.contains(el)) continue;
        if (c.querySelector("input, select, textarea")) continue;
        const t = textOf(c);
        if (t && t.length < 500) return t;
      }
    }
    return "";
  }

  function isRequired(el, label) {
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    return /\*\s*$|\*\s|\(required\)/i.test(label || "");
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
  function baseKey(el, label) {
    if (el.name && !/^\d+$/.test(el.name)) return el.name;
    if (!unstableId(el.id)) return el.id;
    return "label:" + slug(label || el.getAttribute("placeholder") || el.type || "field");
  }

  const root = document;
  const controls = Array.from(root.querySelectorAll("input, select, textarea"));
  const seenGroups = new Set();

  for (const el of controls) {
    const type = (el.getAttribute("type") || el.tagName).toLowerCase();
    if (["hidden", "submit", "button", "reset", "image", "search"].includes(type)) continue;
    if (el.closest("[aria-hidden='true']") && type !== "file") continue;
    if (el.disabled) continue;
    // Skip captcha internals and honeypots.
    if (el.closest("[class*='captcha' i], [id*='captcha' i]")) continue;
    if (type !== "file" && type !== "checkbox" && type !== "radio" && !isVisible(el)) continue;

    if (type === "radio" || type === "checkbox") {
      const groupName = el.name || "";
      const groupKey = `${type}:${groupName}`;
      if (groupName && seenGroups.has(groupKey)) continue;
      const members = groupName
        ? Array.from(root.querySelectorAll(`input[type="${type}"][name="${CSS.escape(groupName)}"]`))
        : [el];
      seenGroups.add(groupKey);
      const options = members.map((m) => {
        let t = "";
        if (m.id) t = textOf(document.querySelector(`label[for="${CSS.escape(m.id)}"]`));
        if (!t) t = textOf(m.closest("label"));
        if (!t) t = clean(m.getAttribute("aria-label")) || clean(m.value);
        return t;
      });
      const container =
        members.length > 1
          ? (function common(a, b) {
              let p = a.parentElement;
              while (p && !p.contains(b)) p = p.parentElement;
              return p || a.parentElement;
            })(members[0], members[members.length - 1])
          : el.parentElement;
      let label = containerLabel(members[0]);
      if (members.length === 1 && type === "checkbox") label = label || options[0];
      const kind = type === "radio" ? "radio" : members.length > 1 ? "checkbox_group" : "checkbox";
      const key = groupName && !/^\d+$/.test(groupName) ? groupName : "label:" + slug(label || options.join("_"));
      addField(
        {
          key,
          kind,
          label: stripRequired(label),
          required: members.some((m) => isRequired(m, label)) || /\*/.test(label),
          options,
          name: groupName,
        },
        container
      );
      continue;
    }

    const label = labelFor(el);
    let kind;
    if (el.tagName === "SELECT") kind = "select";
    else if (el.tagName === "TEXTAREA") kind = "textarea";
    else if (el.getAttribute("role") === "combobox" || el.closest("[class*='select__' i], [class*='combobox' i]"))
      kind = "combobox";
    else if (type === "file") kind = "file";
    else kind = ["email", "tel", "url", "number", "date"].includes(type) ? type : "text";

    const f = {
      key: baseKey(el, label),
      kind,
      label: stripRequired(label),
      required: isRequired(el, label),
      name: el.name || "",
      id: el.id || "",
      placeholder: clean(el.getAttribute("placeholder")),
    };
    if (kind === "select") {
      f.options = Array.from(el.options)
        .map((o) => clean(o.textContent))
        .filter((t) => t && !/^(select|choose|--)/i.test(t));
      f.multiple = el.multiple;
    }
    if (kind === "file") f.accept = el.getAttribute("accept") || "";
    addField(f, el);
  }

  // Button groups (e.g. Ashby's Yes/No questions rendered as <button>s).
  const groupContainers = new Set();
  for (const btn of root.querySelectorAll("button[type='button']")) {
    const parent = btn.parentElement;
    if (!parent || groupContainers.has(parent)) continue;
    const buttons = Array.from(parent.children).filter((c) => c.tagName === "BUTTON");
    if (buttons.length < 2 || buttons.length > 6) continue;
    const opts = buttons.map(textOf);
    if (!opts.every((t) => t && t.length <= 40)) continue;
    if (!opts.some((t) => /^(yes|no)$/i.test(t))) continue;
    groupContainers.add(parent);
    const label = containerLabel(parent);
    addField(
      {
        key: "label:" + slug(label || opts.join("_")),
        kind: "button_group",
        label: stripRequired(label),
        required: /\*/.test(label),
        options: opts,
      },
      parent
    );
  }

  const submit =
    root.querySelector("button[type='submit'], input[type='submit']") ||
    Array.from(root.querySelectorAll("button")).find((b) => /submit/i.test(textOf(b)));
  if (submit) submit.setAttribute("data-ja-submit", "1");

  const captcha = {
    recaptcha: !!root.querySelector("iframe[src*='recaptcha'], .g-recaptcha, script[src*='recaptcha']"),
    hcaptcha: !!root.querySelector("iframe[src*='hcaptcha'], .h-captcha, script[src*='hcaptcha']"),
    turnstile: !!root.querySelector("iframe[src*='turnstile'], .cf-turnstile, script[src*='turnstile']"),
  };

  return { url: location.href, title: document.title, fields, has_submit: !!submit, captcha };
};
