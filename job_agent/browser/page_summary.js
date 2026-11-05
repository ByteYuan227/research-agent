// Summarise the current page for an agent that drives the browser step by step:
// clickable elements (tagged with data-ja-ref so they can be clicked by ref),
// headings, alerts and the visible text of the main content.
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  document.querySelectorAll("[data-ja-ref]").forEach((e) => e.removeAttribute("data-ja-ref"));
  const sel = "button, a[href], [role=button], [role=link], [role=tab], [role=menuitem], input[type=submit], input[type=button]";
  const clickables = [];
  const seen = new Set();
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (!visible(el)) continue;
    if (el.matches("[aria-haspopup=listbox]")) continue; // a dropdown field, not an action
    const group = el.closest("[data-ja-kind='button_group']");
    if (group) continue; // Yes/No answer buttons are a field, filled with `web fill`
    let text = clean(el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title"));
    if (!text) continue;
    text = text.slice(0, 90);
    const href = el.tagName === "A" ? el.getAttribute("href") || "" : "";
    const dedupe = text + "|" + href;
    if (seen.has(dedupe)) continue;
    seen.add(dedupe);
    const ref = "r" + ++n;
    el.setAttribute("data-ja-ref", ref);
    clickables.push({
      ref,
      text,
      kind: el.tagName === "A" ? "link" : "button",
      href: /^javascript:/i.test(href) ? "" : href.slice(0, 140),
      disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
    });
    if (n >= 150) break;
  }
  const headings = Array.from(document.querySelectorAll("h1, h2, h3, [role=heading]"))
    .filter(visible)
    .map((h) => clean(h.innerText))
    .filter(Boolean)
    .slice(0, 15);
  const alerts = Array.from(
    document.querySelectorAll("[role=alert], [aria-live=assertive], .error, .error-message, [class*='errorMessage' i], [data-automation-id*='error' i]")
  )
    .filter(visible)
    .map((e) => clean(e.innerText))
    .filter((t) => t && t.length < 300)
    .slice(0, 12);
  const main = document.querySelector("main, [role=main]") || document.body;
  return { clickables, headings, alerts, text: clean(main.innerText).slice(0, 3500) };
};
