from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

from . import ai


# Pass 1 — combobox detection. We tag visible role="combobox" inputs so we can drive them
# from Python (open, read [role=option] entries, close). The accompanying aria-hidden text
# inputs that hold the form value are also tagged so the standard pass skips them.
COMBOBOX_TAG_JS = r"""
() => {
  const out = [];
  let i = 0;
  const isVisible = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (el.offsetParent === null && cs.position !== 'fixed') return false;
    return true;
  };
  const cleanText = (el) => {
    if (!el) return null;
    const required = el.querySelector?.('.required, [aria-label="required"]');
    if (required) required.remove();
    const t = (el.textContent || '').replace(/\s+/g, ' ').replace(/\*+$/, '').replace(/\(required\)$/i, '').trim();
    return t || null;
  };

  document.querySelectorAll('[role="combobox"]').forEach(el => {
    if (!isVisible(el)) return;
    if (el.closest('[role="listbox"]')) return;  // search-inside-dropdown
    let label = null;
    const lblBy = el.getAttribute('aria-labelledby');
    if (lblBy) {
      const lbl = document.getElementById(lblBy);
      if (lbl) label = cleanText(lbl);
    }
    if (!label) label = el.getAttribute('aria-label');
    if (!label) {
      const wrap = el.closest('.field-wrapper, .field, fieldset, [data-testid]');
      const lbl = wrap?.querySelector('legend, label, h2, h3, h4, [class*="label"]');
      if (lbl) label = cleanText(lbl);
    }
    const required = (label || '').endsWith('*') || el.required;
    const key = `aecb_${i++}`;
    el.setAttribute('data-ae-key', key);
    el.setAttribute('data-ae-combobox', '1');
    out.push({ key, label: (label || '').replace(/\*+$/, '').trim(), required });

    // Tag the sibling aria-hidden shadow input that holds the value, so the standard pass skips it.
    const wrap = el.closest('.field-wrapper, .field, fieldset, [data-testid]') || el.parentElement;
    if (wrap) {
      wrap.querySelectorAll('input[aria-hidden="true"]').forEach(s => {
        s.setAttribute('data-ae-skip', '1');
      });
    }
  });
  return out;
}
"""


# Pass 2 — standard inputs/selects/textareas/file/radio/checkbox.
EXTRACT_JS = r"""
() => {
  const cleanText = (el) => {
    if (!el) return null;
    const clone = el.cloneNode(true);
    clone.querySelectorAll('.required, [aria-label="required"]').forEach(n => n.remove());
    let t = clone.textContent || '';
    t = t.replace(/\s+/g, ' ').replace(/\*+$/, '').replace(/\(required\)$/i, '').trim();
    return t || null;
  };

  const isVisible = (el) => {
    if (el.type === 'file') return true;  // file inputs are often visually hidden behind a styled label
    if (el.type === 'hidden') return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (el.offsetParent === null && cs.position !== 'fixed') return false;
    return true;
  };

  const labelFor = (input) => {
    if (input.id) {
      try {
        const lbl = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
        if (lbl) {
          const txt = cleanText(lbl);
          if (txt && !/^(attach|upload|choose file|browse)$/i.test(txt)) return txt;
        }
      } catch (e) {}
    }
    const parentLabel = input.closest('label');
    if (parentLabel) {
      const txt = cleanText(parentLabel);
      if (txt && !/^(attach|upload|choose file|browse)$/i.test(txt)) return txt;
    }
    const aria = input.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledBy = input.getAttribute('aria-labelledby');
    if (labelledBy) {
      const lbl = document.getElementById(labelledBy);
      if (lbl) return cleanText(lbl);
    }
    // Walk up to a field wrapper and look for a heading/legend.
    const wrap = input.closest('.field-wrapper, .field, .form-field, fieldset, [data-testid], [class*="question"], [class*="Field"]');
    if (wrap) {
      const heading = wrap.querySelector('legend, h2, h3, h4, label, [class*="label"]');
      if (heading) {
        const txt = cleanText(heading);
        if (txt) return txt;
      }
    }
    return null;
  };

  const groupLabel = (radio) => {
    const fs = radio.closest('fieldset');
    if (fs) {
      const legend = fs.querySelector('legend');
      if (legend) return cleanText(legend);
    }
    const wrap = radio.closest('.field-wrapper, .field, [data-testid], [class*="question"], [class*="Field"]');
    if (wrap) {
      const heading = wrap.querySelector('legend, h2, h3, h4, label, [class*="label"]');
      if (heading) return cleanText(heading);
    }
    return null;
  };

  // Prefer known Greenhouse identifiers; otherwise pick the form with the most
  // non-hidden inputs (application forms have many fields; search/nav forms have few).
  let form = document.querySelector(
    '#grnhse_app form, form#application_form, form#application-form, form[action*="greenhouse.io"]'
  );
  if (!form) {
    let best = null, bestCount = -1;
    for (const f of document.querySelectorAll('form')) {
      const n = f.querySelectorAll(
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), select, textarea'
      ).length;
      if (n > bestCount) { bestCount = n; best = f; }
    }
    form = best;
  }
  if (!form) return [];

  const fields = [];
  const seen = new Set();
  let counter = 0;
  const nextKey = () => `ae_${++counter}`;

  // Skip already-tagged combobox inputs and their shadows.
  form.querySelectorAll('[data-ae-key], [data-ae-skip="1"]').forEach(el => seen.add(el));

  // Radio groups
  const radioGroups = new Map();
  form.querySelectorAll('input[type="radio"]').forEach(r => {
    if (seen.has(r)) return;
    if (!r.name) return;
    if (!radioGroups.has(r.name)) radioGroups.set(r.name, []);
    radioGroups.get(r.name).push(r);
  });
  for (const [name, radios] of radioGroups) {
    const key = nextKey();
    const label = groupLabel(radios[0]) || name;
    const options = [];
    radios.forEach(r => {
      const opt = labelFor(r) || r.value;
      r.setAttribute('data-ae-key', key);
      r.setAttribute('data-ae-radio-value', opt);
      options.push(opt);
      seen.add(r);
    });
    fields.push({ key, type: 'radio', label, name, required: radios.some(r => r.required), options, maxLength: null });
  }

  // Checkbox groups (multiple checkboxes sharing a name) vs single boolean checkbox
  const checkboxGroups = new Map();
  form.querySelectorAll('input[type="checkbox"]').forEach(c => {
    if (seen.has(c)) return;
    const name = c.name;
    if (!name) {
      const key = nextKey();
      c.setAttribute('data-ae-key', key);
      const label = labelFor(c);
      if (label) fields.push({ key, type: 'checkbox', label, name: c.name, required: c.required, options: ['Yes', 'No'], maxLength: null });
      seen.add(c);
      return;
    }
    if (!checkboxGroups.has(name)) checkboxGroups.set(name, []);
    checkboxGroups.get(name).push(c);
  });
  for (const [name, boxes] of checkboxGroups) {
    if (boxes.length === 1) {
      const c = boxes[0];
      const key = nextKey();
      c.setAttribute('data-ae-key', key);
      const label = labelFor(c);
      if (label) fields.push({ key, type: 'checkbox', label, name, required: c.required, options: ['Yes', 'No'], maxLength: null });
      seen.add(c);
      continue;
    }
    const key = nextKey();
    const label = groupLabel(boxes[0]) || name;
    const options = [];
    boxes.forEach(c => {
      const opt = labelFor(c) || c.value;
      c.setAttribute('data-ae-key', key);
      c.setAttribute('data-ae-checkbox-value', opt);
      options.push(opt);
      seen.add(c);
    });
    fields.push({ key, type: 'multiselect', label, name, required: boxes.some(c => c.required), options, maxLength: null });
  }

  // Standard text/select/textarea/file
  form.querySelectorAll('input, select, textarea').forEach(el => {
    if (seen.has(el)) return;
    if (el.disabled) return;
    if (!isVisible(el)) return;
    if (el.type === 'submit' || el.type === 'button' || el.type === 'reset' || el.type === 'hidden') return;
    if (el.tagName === 'BUTTON') return;
    if (el.getAttribute('aria-hidden') === 'true') return;
    if (el.type === 'search') return;  // search-inside-dropdown helpers

    const tag = el.tagName.toLowerCase();
    let type = el.type || tag;
    let options = null;

    if (tag === 'select') {
      type = el.multiple ? 'multiselect' : 'select';
      options = Array.from(el.options).map(o => (o.text || '').trim())
        .filter(t => t && !/^(select|please select|choose|--)/i.test(t));
    } else if (type === 'file') {
      type = 'file';
    } else if (tag === 'textarea') {
      type = 'textarea';
    } else {
      const allowed = new Set(['text', 'email', 'tel', 'url', 'number', 'date']);
      if (!allowed.has(type)) type = 'text';
      if (type === 'tel') type = 'phone';
    }

    const label = labelFor(el);
    if (!label && type !== 'file') return;

    const key = nextKey();
    el.setAttribute('data-ae-key', key);

    fields.push({
      key, type,
      label: label || 'File upload',
      name: el.name,
      required: el.required || false,
      options,
      maxLength: el.maxLength > 0 ? el.maxLength : null,
    });
    seen.add(el);
  });

  return fields;
}
"""


@dataclass
class Field:
    key: str
    type: str  # text, textarea, email, phone, url, number, date, select, multiselect, radio, checkbox, file, searchable_select
    label: str
    name: str | None
    required: bool
    options: list[str] | None
    max_length: int | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Field":
        return cls(
            key=d["key"], type=d["type"], label=d["label"], name=d.get("name"),
            required=d.get("required", False),
            options=d.get("options"), max_length=d.get("maxLength"),
        )

    def to_field_spec(self) -> ai.FieldSpec:
        return ai.FieldSpec(
            question=self.label,
            field_type=self.type,
            options=self.options,
            required=self.required,
            max_length=self.max_length,
        )


@dataclass
class PageMeta:
    title: str | None
    company: str | None
    location: str | None = None


# How many options before we treat a combobox as a free-text searchable field
# (eg country lists with 200+ entries — too many to enumerate to the model).
SEARCHABLE_THRESHOLD = 25


_DISMISS_OVERLAYS_JS = r"""
() => {
  // Try to close common cookie/consent/sticky banners that intercept pointer events.
  const acceptRe = /^(accept|close|dismiss|got it|okay|ok|i agree|agree|continue|confirm|allow)/i;

  // Known IDs / classes that appear on top of forms
  const bannerRoots = Array.from(document.querySelectorAll(
    '#relyance-banner-container, #gtmStickyBanner, '
    + '[id*="cookie"], [id*="consent"], [id*="gdpr"], [id*="privacy-banner"], '
    + '[class*="cookie-banner"], [class*="cookieBanner"], [class*="consent-banner"], '
    + '[class*="privacy-banner"], [class*="sticky-banner"]'
  ));

  for (const root of bannerRoots) {
    const cs = getComputedStyle(root);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;

    // First try clicking a close/accept button inside it
    const btns = Array.from(root.querySelectorAll('button, [role="button"], a[role="button"]'));
    let clicked = false;
    for (const btn of btns) {
      const lbl = (btn.getAttribute('aria-label') || '').trim();
      const txt = (btn.textContent || '').replace(/\s+/g, ' ').trim();
      if (acceptRe.test(txt) || /close|dismiss|accept/i.test(lbl)) {
        try { btn.click(); } catch (e) {}
        clicked = true;
        break;
      }
    }

    if (!clicked) {
      // If there is no obvious button, just neutralise pointer interception
      root.style.setProperty('pointer-events', 'none', 'important');
      // Also hide dialogs inside (they block scroll-into-view too)
      root.querySelectorAll('[role="dialog"]').forEach(d => {
        d.style.setProperty('pointer-events', 'none', 'important');
      });
    }
  }

  // Catch-all: any fixed/sticky modal dialog that is NOT the application form itself
  document.querySelectorAll('[role="dialog"][aria-modal="true"]').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') return;
    // If it is not inside a <form>, it is an overlay — neutralise it
    if (!el.closest('form')) {
      el.style.setProperty('pointer-events', 'none', 'important');
    }
  });
}
"""


def _dismiss_overlays(page: Page) -> None:
    """Best-effort removal of cookie/consent/sticky banners that block form interaction."""
    try:
        page.evaluate(_DISMISS_OVERLAYS_JS)
        page.wait_for_timeout(300)
    except Exception:
        pass


def _find_greenhouse_iframe_url(page: Page) -> str | None:
    """Return the URL of any Greenhouse embed iframe/frame on the page, or None.

    Uses Playwright's native frame registry first (catches dynamically-created iframes
    that may not yet have their src attribute set in the DOM), then falls back to a JS
    scan of the DOM for iframes whose src property matches greenhouse.io."""
    # Native frame list — most reliable; Playwright registers frames as they're created.
    for frame in page.frames:
        url = frame.url
        if "greenhouse.io" in url and url != page.url:
            return url
    # JS fallback for iframes that exist in the DOM but haven't become separate frames yet.
    return page.evaluate(
        r"""() => {
            for (const f of document.querySelectorAll('iframe')) {
                const src = f.src || f.getAttribute('data-src') || '';
                if (/greenhouse\.io/i.test(src)) return src;
            }
            return null;
        }"""
    )


def _try_follow_apply_link(page: Page) -> "Page | None":
    """On a job-description page with no form fields, look for an apply link/button and
    follow it. Returns the page containing the application form (same page after navigation
    or a newly-opened tab), or None if no navigation occurred."""
    # 1. Direct link to greenhouse.io in page HTML — navigate immediately
    gh_link: str | None = page.evaluate(
        r"""() => {
            const applyRe = /apply/i;
            for (const a of document.querySelectorAll('a')) {
                const href = a.href || '';
                if (!/greenhouse\.io/i.test(href)) continue;
                const txt = (a.textContent || '').replace(/\s+/g, ' ').trim();
                if (applyRe.test(txt) || applyRe.test(href)) return href;
            }
            const any = document.querySelector('a[href*="greenhouse.io"]');
            return any ? any.href : null;
        }"""
    )
    if gh_link:
        page.goto(gh_link, wait_until="domcontentloaded")
        return page

    # 2. Click an "Apply" button/link; handle both new-tab and same-tab navigation
    apply_btns = page.locator(
        'a:has-text("Apply for this job"), button:has-text("Apply for this job"), '
        'a:has-text("Apply Now"), button:has-text("Apply Now"), '
        'a:has-text("Apply"), button:has-text("Apply")'
    )
    if apply_btns.count() == 0:
        return None

    prev_url = page.url
    context = page.context
    try:
        with context.expect_page(timeout=3000) as new_page_event:
            apply_btns.first.click(timeout=5000)
        new_page = new_page_event.value
        new_page.wait_for_load_state("domcontentloaded")
        return new_page
    except PlaywrightTimeout:
        pass  # No new tab; check same-tab navigation below
    except Exception:
        return None

    try:
        page.wait_for_url(lambda u: u != prev_url, timeout=5000)
    except PlaywrightTimeout:
        pass

    if page.url == prev_url:
        return None  # No navigation occurred

    # May have landed on a company page that still embeds Greenhouse via iframe
    if "greenhouse.io" not in page.url:
        try:
            page.wait_for_selector("iframe[src*='greenhouse.io']", timeout=5000)
        except PlaywrightTimeout:
            pass
        gh_iframe_url = _find_greenhouse_iframe_url(page)
        if gh_iframe_url:
            page.goto(gh_iframe_url, wait_until="domcontentloaded")

    return page


def open_application(page_factory, url: str) -> tuple[Page, list[Field], PageMeta]:
    page = page_factory()
    page.goto(url, wait_until="domcontentloaded")

    # If the host page is not itself a Greenhouse domain, wait for all JS to settle
    # (networkidle) — Greenhouse JS embeds inject the form only after fetching the job
    # definition, so they appear well after DOMContentLoaded. networkidle is the right
    # signal rather than a fixed timeout.
    if "greenhouse.io" not in page.url:
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightTimeout:
            pass
        gh_iframe_url = _find_greenhouse_iframe_url(page)
        if gh_iframe_url:
            page.goto(gh_iframe_url, wait_until="domcontentloaded")

    # Wait for Greenhouse-specific form elements. Generic "form input" fires immediately
    # from search/nav forms; these selectors only appear once the application form loads.
    try:
        page.wait_for_selector(
            "form#application_form, #grnhse_app form, "
            "input[name='first_name'], input[name='email']",
            timeout=12000,
        )
    except PlaywrightTimeout:
        # Fall back to any form input if the above never appear.
        try:
            page.wait_for_selector(
                "form input, form select, form textarea, form [role='combobox']",
                timeout=5000,
            )
        except PlaywrightTimeout:
            pass
    time.sleep(0.3)
    _dismiss_overlays(page)

    combobox_fields = _extract_comboboxes(page)
    standard = page.evaluate(EXTRACT_JS)
    standard_fields = [Field.from_dict(d) for d in standard]

    # If no application fields found, this is likely a job-description page.
    # Follow the "Apply" link/button to reach the actual application form.
    if not combobox_fields and not standard_fields:
        form_page = _try_follow_apply_link(page)
        if form_page is not None:
            page = form_page
            try:
                page.wait_for_selector(
                    "form input, form select, form textarea, form [role='combobox']",
                    timeout=15000,
                )
            except PlaywrightTimeout:
                pass
            time.sleep(0.6)
            _dismiss_overlays(page)
            combobox_fields = _extract_comboboxes(page)
            standard = page.evaluate(EXTRACT_JS)
            standard_fields = [Field.from_dict(d) for d in standard]

    return page, combobox_fields + standard_fields, _extract_meta(page)


def _extract_comboboxes(page: Page) -> list[Field]:
    raw = page.evaluate(COMBOBOX_TAG_JS)
    fields: list[Field] = []
    for box in raw:
        sel = f'[data-ae-key="{box["key"]}"]'
        options: list[str] = []
        try:
            page.locator(sel).first.click()
            page.wait_for_timeout(250)
            options = _read_visible_options(page)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        except Exception:
            options = []

        # Trim duplicates while preserving order
        seen = set()
        opts: list[str] = []
        for o in options:
            if o not in seen:
                seen.add(o)
                opts.append(o)

        # Empty option set OR a very long one = searchable. Empty usually means the
        # combobox needs the user to type something to filter (city pickers, etc.).
        searchable = len(opts) == 0 or len(opts) > SEARCHABLE_THRESHOLD
        fields.append(
            Field(
                key=box["key"],
                type="searchable_select" if searchable else "select",
                label=box["label"] or "Combobox",
                name=None,
                required=box.get("required", False),
                options=None if searchable else opts,
                max_length=None,
            )
        )
    return fields


def _extract_meta(page: Page) -> PageMeta:
    title = page.title() or None
    company = None
    location = None
    try:
        company = page.evaluate(
            """() => {
                const sel = ['.company-name', '#header h1', 'header h1', '[class*="company"]'];
                for (const s of sel) {
                    const el = document.querySelector(s);
                    if (el && el.textContent.trim()) return el.textContent.trim();
                }
                return null;
            }"""
        )
    except Exception:
        pass
    try:
        location = page.evaluate(
            """() => {
                // 1. Dedicated location elements
                const sel = [
                    '.location', '[class*="location"]', '[class*="job-location"]',
                    '[data-qa="job-location"]', '.posting-location', '.job__location',
                    '.header__location', '.job-header__location', 'span[itemprop="addressLocality"]',
                    '[itemtype*="JobPosting"] [itemprop="jobLocation"]',
                ];
                for (const s of sel) {
                    const el = document.querySelector(s);
                    const t = el && el.textContent.trim();
                    if (t) return t;
                }
                // 2. <meta> tags (og:description or structured data sometimes carry it)
                const metas = document.querySelectorAll('meta[name="description"], meta[property="og:description"]');
                for (const m of metas) {
                    const c = (m.getAttribute('content') || '').trim();
                    if (c) return c;  // caller will use this as a hint only
                }
                return null;
            }"""
        )
    except Exception:
        pass

    # 3. Fall back to page title — Greenhouse titles are often "Role at Company in City, Country"
    if not location and title:
        import re as _re
        m = _re.search(r'\bin\s+([A-Z][^|–—·]+)', title)
        if m:
            location = m.group(1).strip()

    return PageMeta(title=title, company=company, location=location)


def fill_field(page: Page, field: Field, value: str, resume_path: Path | None = None) -> str | None:
    selector = f'[data-ae-key="{field.key}"]'

    if field.type == "file":
        if resume_path and re.search(r"resume|cv\b|curriculum", field.label, re.I):
            page.locator(selector).first.set_input_files(str(resume_path))
            return "uploaded"
        return "skipped"

    if field.type in {"text", "email", "phone", "url", "number", "date", "textarea"}:
        loc = page.locator(selector).first
        try:
            loc.click(timeout=5000)
        except Exception:
            # An overlay is likely intercepting — neutralise it and force the click.
            _dismiss_overlays(page)
            loc.click(force=True)
        loc.fill("")  # clear pre-filled value
        # Greenhouse location/city inputs are autocomplete typeaheads — typing alone
        # leaves the form value empty until you pick a suggestion.
        if _is_location_field(field) and value:
            _fill_location(page, loc, value)
            return
        # Slow type for short fields (cadence helps reCAPTCHA score); fill() for long content.
        if field.type == "textarea" or len(value) > 80:
            loc.fill(value)
        else:
            loc.type(value, delay=40)
        return

    if field.type == "select":
        # Combobox-as-select OR native <select>
        first = page.locator(selector).first
        is_combo = first.get_attribute("data-ae-combobox") == "1"
        if is_combo:
            _pick_combobox_option(page, selector, value)
        else:
            try:
                first.select_option(label=value)
            except Exception:
                first.select_option(value=value)
        return

    if field.type == "searchable_select":
        _type_and_pick(page, selector, value)
        return

    if field.type == "multiselect":
        try:
            values = json.loads(value) if value.strip().startswith("[") else [value]
        except json.JSONDecodeError:
            values = [value]
        first = page.locator(selector).first
        tag = first.evaluate("e => e.tagName.toLowerCase()")
        if tag == "select":
            try:
                first.select_option(label=values)
            except Exception:
                first.select_option(value=values)
            return
        all_boxes = page.locator(selector)
        for i in range(all_boxes.count()):
            box = all_boxes.nth(i)
            opt = box.get_attribute("data-ae-checkbox-value") or ""
            should_check = any(opt.strip() == v.strip() for v in values)
            if should_check and not box.is_checked():
                box.check()
            elif not should_check and box.is_checked():
                box.uncheck()
        return

    if field.type == "radio":
        target = page.locator(f'{selector}[data-ae-radio-value="{_escape_attr(value)}"]')
        if target.count() == 0:
            all_radios = page.locator(selector)
            for i in range(all_radios.count()):
                r = all_radios.nth(i)
                opt = r.get_attribute("data-ae-radio-value") or ""
                if opt.strip().lower() == value.strip().lower():
                    target = r
                    break
        target.first.check()
        return

    if field.type == "checkbox":
        loc = page.locator(selector).first
        truthy = value.strip().lower() in {"yes", "true", "1", "on"}
        if truthy and not loc.is_checked():
            loc.check()
        elif not truthy and loc.is_checked():
            loc.uncheck()
        return

    raise ValueError(f"Unhandled field type: {field.type}")


_VISIBLE_OPTIONS_JS = r"""
() => {
  const visible = Array.from(document.querySelectorAll('[role="listbox"]'))
    .filter(lb => lb.offsetParent !== null);
  const opts = [];
  visible.forEach(lb => {
    lb.querySelectorAll('[role="option"]').forEach(o => {
      const t = (o.textContent || '').replace(/\s+/g, ' ').trim();
      if (t) opts.push(t);
    });
  });
  return opts;
}
"""


def _read_visible_options(page: Page) -> list[str]:
    """Read options only from listboxes that are actually rendered (offsetParent != null).
    This excludes the international-tel-input hidden country listbox."""
    raw = page.evaluate(_VISIBLE_OPTIONS_JS)
    out: list[str] = []
    seen: set[str] = set()
    for o in raw:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


def _click_visible_option(page: Page, value: str) -> bool:
    """Click an option matching value within a visible listbox. Returns True if clicked.

    Match priority: exact -> startsWith -> word-boundary -> substring. The naive
    `includes` fallback alone matches 'British Indian Ocean Territory' for 'India',
    so we always prefer a word-boundary match before falling back to substring.
    """
    target_lower = value.strip().lower()
    return page.evaluate(
        r"""(target) => {
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const escaped = target.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&');
            const wordRe = new RegExp('\\b' + escaped + '\\b', 'i');
            const visible = Array.from(document.querySelectorAll('[role="listbox"]'))
                .filter(lb => lb.offsetParent !== null);
            for (const lb of visible) {
                const opts = Array.from(lb.querySelectorAll('[role="option"]'));
                if (!opts.length) continue;
                let match = opts.find(o => norm(o.textContent) === target);
                if (!match) match = opts.find(o => norm(o.textContent).startsWith(target));
                if (!match) match = opts.find(o => wordRe.test(norm(o.textContent)));
                if (!match) match = opts.find(o => norm(o.textContent).includes(target));
                if (match) { match.click(); return true; }
            }
            return false;
        }""",
        target_lower,
    )


def _pick_combobox_option(page: Page, selector: str, value: str) -> None:
    page.locator(selector).first.click()
    page.wait_for_timeout(250)
    if not _click_visible_option(page, value):
        if not _click_visible_option(page, "Other"):
            page.keyboard.press("Escape")
            raise ValueError(f"No combobox option matches {value!r}")


def _type_and_pick(page: Page, selector: str, value: str) -> None:
    """For long-list comboboxes (countries / city autocompletes): focus, type, then poll
    for the dropdown to populate before clicking the best-matching option."""
    loc = page.locator(selector).first
    loc.click()
    loc.fill("")
    loc.press_sequentially(value, delay=60)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if _click_visible_option(page, value):
            page.wait_for_timeout(150)  # let React commit the selection
            return
        page.wait_for_timeout(150)
    # Value not in dropdown — clear and try "Other" as fallback (e.g. unlisted schools).
    loc.fill("")
    loc.press_sequentially("Other", delay=60)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if _click_visible_option(page, "Other"):
            page.wait_for_timeout(150)
            return
        page.wait_for_timeout(150)
    raise ValueError(f"No combobox option matched {value!r} after typing")


def _is_location_field(field: "Field") -> bool:
    return bool(re.search(r"\b(location|city|town)\b", field.label, re.I))


_AUTOCOMPLETE_PICK_JS = r"""
(target) => {
  const t = (target || '').trim().toLowerCase();
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    if (!el || el.offsetParent === null) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  };

  // Cast a wide net — Greenhouse location autocompletes have varied across their
  // releases, and the dropdown is sometimes portaled to <body>.
  const selectors = [
    '[role="listbox"] [role="option"]',
    '[role="option"]',
    'ul[class*="suggest" i] li',
    'ul[class*="autocomplete" i] li',
    'ul[class*="location" i] li',
    '[class*="suggestions" i] li',
    '[class*="dropdown" i] [class*="option" i]',
    '[class*="dropdown" i] [class*="item" i]',
    '[class*="menu" i] [class*="item" i]',
    '.pac-container .pac-item',
    '.geosuggest__item',
    '[id*="downshift" i] li',
  ];
  const candidates = Array.from(document.querySelectorAll(selectors.join(', ')))
    .filter(isVisible)
    .filter(el => norm(el.textContent).length > 0);
  if (!candidates.length) return null;

  // Match by content — never blindly click the first candidate, since [role=option]
  // elements can come from unrelated comboboxes elsewhere on the page.
  let pick = candidates.find(o => norm(o.textContent) === t);
  if (!pick) pick = candidates.find(o => norm(o.textContent).startsWith(t));
  if (!pick) pick = candidates.find(o => norm(o.textContent).includes(t));
  if (!pick) return null;
  pick.scrollIntoView({ block: 'center' });
  pick.click();
  return norm(pick.textContent);
}
"""


def _fill_location(page: Page, loc, value: str) -> bool:
    """Type a location and commit a suggestion. Polls for the dropdown for up to ~3s
    because Greenhouse debounces the autocomplete request. Returns True if a suggestion
    was committed."""
    loc.press_sequentially(value, delay=80)
    deadline = time.time() + 3.0
    picked: str | None = None
    while time.time() < deadline:
        try:
            picked = page.evaluate(_AUTOCOMPLETE_PICK_JS, value)
        except Exception:
            picked = None
        if picked:
            break
        page.wait_for_timeout(200)
    if not picked:
        return False
    # Tab off so the React component locks in the selection before we move on.
    loc.press("Tab")
    page.wait_for_timeout(150)
    return True


def _escape_attr(s: str) -> str:
    return s.replace('"', '\\"')


def find_security_code_field(page: Page, wait_ms: int = 0) -> str | None:
    """If a Greenhouse-style email security-code input is present on the page, return a CSS
    selector that targets it. Polls up to `wait_ms` for it to appear (post-submit DOM updates).
    Otherwise None."""
    import time as _t
    deadline = _t.time() + (wait_ms / 1000.0)
    while True:
        sel = _find_security_code_field_now(page)
        if sel:
            return sel
        if _t.time() >= deadline:
            return None
        page.wait_for_timeout(500)


def _find_security_code_field_now(page: Page) -> str | None:
    return page.evaluate(
        r"""() => {
            const cleanText = (el) => (el?.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const inputs = Array.from(document.querySelectorAll('input'));
            for (const inp of inputs) {
                if (inp.type === 'hidden' || inp.type === 'submit' || inp.disabled) continue;
                const attrs = [
                    inp.name, inp.id, inp.placeholder || '',
                    inp.getAttribute('aria-label') || '',
                ].join(' ').toLowerCase();
                if (/security.?code|verification.?code|confirm.?code|one.?time.?code|auth.?code/.test(attrs)) {
                    if (!inp.id) inp.id = 'ae_security_code_' + Math.random().toString(36).slice(2,8);
                    return '#' + CSS.escape(inp.id);
                }
                // Label-for relationship
                if (inp.id) {
                    const lbl = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
                    if (lbl && /security.?code|verification.?code/i.test(cleanText(lbl))) {
                        return '#' + CSS.escape(inp.id);
                    }
                }
                // Nearest label-ish ancestor
                const wrap = inp.closest('.field-wrapper, .field, fieldset');
                if (wrap) {
                    const lbl = wrap.querySelector('legend, label, [class*="label"]');
                    if (lbl && /security.?code|verification.?code/i.test(cleanText(lbl))) {
                        if (!inp.id) inp.id = 'ae_security_code_' + Math.random().toString(36).slice(2,8);
                        return '#' + CSS.escape(inp.id);
                    }
                }
            }
            return null;
        }"""
    )


def has_recaptcha(page: Page) -> bool:
    """True if the page has any common bot-protection widget that will silently block submit."""
    return page.evaluate(
        """() => !!document.querySelector(
            '.grecaptcha-badge, .grecaptcha-logo, iframe[src*="recaptcha"], '
            'iframe[src*="hcaptcha"], [class*="hcaptcha"], [class*="cf-turnstile"]'
        )"""
    )


def submit(page: Page) -> str:
    """Click submit, wait for the post-submit DOM to settle into one of the known states,
    and return 'verified', 'unverified', 'blocked', or 'code_required'."""
    _dismiss_overlays(page)
    # Exclude inputs with the HTML `hidden` attribute (e.g. search form submit buttons).
    btn_loc = page.locator(
        'form button[type="submit"], '
        'form input[type="submit"]:not([hidden]), '
        'button:has-text("Submit Application"), '
        'button:has-text("Submit application"), '
        'button:has-text("Submit")'
    )
    # Walk candidates in DOM order and pick the first one that is actually visible.
    btn = btn_loc.first
    for i in range(min(btn_loc.count(), 10)):
        candidate = btn_loc.nth(i)
        try:
            if candidate.is_visible():
                btn = candidate
                break
        except Exception:
            continue
    btn.click()
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeout:
        pass

    # Wait up to 15s for one of: thank-you text, security-code field, captcha error.
    try:
        page.wait_for_function(
            r"""() => {
                const text = (document.body.innerText || '').toLowerCase();
                if (/thank you for applying|application (?:has been )?submitted|application received|we've received your application|thanks for applying/.test(text)) return 'verified';
                if (location.href.toLowerCase().includes('thank-you') || location.href.toLowerCase().includes('confirmation')) return 'verified';
                const secInputs = Array.from(document.querySelectorAll('input'));
                for (const inp of secInputs) {
                    if (inp.type === 'hidden' || inp.disabled) continue;
                    const attrs = (inp.name + ' ' + inp.id + ' ' + (inp.placeholder || '') + ' ' + (inp.getAttribute('aria-label') || '')).toLowerCase();
                    if (/security.?code|verification.?code|confirm.?code|one.?time.?code|auth.?code/.test(attrs)) return 'code_required';
                }
                if (document.querySelector('.grecaptcha-error') && document.querySelector('.grecaptcha-error').textContent.trim()) return 'blocked';
                return null;
            }""",
            timeout=15000,
        )
    except PlaywrightTimeout:
        pass

    if find_security_code_field(page, wait_ms=2000):
        return "code_required"
    return verify_submission(page)


def verify_submission(page: Page) -> str:
    """Look for a confirmation marker. 'verified' if we find one, 'blocked' if a captcha
    error is showing, 'invalid' if the form is showing field-level errors, 'unverified' if
    none of the above."""
    state = page.evaluate(
        """() => {
            const text = (document.body.innerText || '').toLowerCase();
            const url = location.href.toLowerCase();
            const success = /thank you for applying|application (?:has been )?submitted|application received|we've received your application|thanks for applying/.test(text)
                || url.includes('thank-you') || url.includes('confirmation') || url.includes('thanks');
            const captchaError = /captcha|robot|verify you are human/.test(text)
                && !!document.querySelector('.grecaptcha-error, [class*="captcha-error"]');
            return { success, captchaError };
        }"""
    )
    if state["success"]:
        return "verified"
    if state["captchaError"]:
        return "blocked"
    if find_form_errors(page):
        return "invalid"
    return "unverified"


_FIND_FORM_ERRORS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const isVisible = (el) => {
    if (!el) return false;
    if (el.offsetParent === null) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').replace(/\*+$/, '').replace(/\(required\)$/i, '').trim();
  const labelOf = (el) => {
    const lblBy = el.getAttribute('aria-labelledby');
    if (lblBy) {
      const lbl = document.getElementById(lblBy);
      if (lbl) { const t = clean(lbl.textContent); if (t) return t; }
    }
    if (el.id) {
      try {
        const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lbl) { const t = clean(lbl.textContent); if (t) return t; }
      } catch (e) {}
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const wrap = el.closest('.field-wrapper, .field, fieldset, [data-testid], [class*="question"], [class*="Field"]');
    if (wrap) {
      const lbl = wrap.querySelector('legend, label, h2, h3, h4, [class*="label"]');
      if (lbl) return clean(lbl.textContent);
    }
    return null;
  };
  const push = (field, message) => {
    const m = clean(message);
    if (!m) return;
    if (m.length > 200) return;
    const key = (field || '') + '||' + m;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ field: field || '?', message: m });
  };

  // 1. aria-invalid inputs — pull error text from aria-describedby or nearby error nodes.
  document.querySelectorAll('input[aria-invalid="true"], select[aria-invalid="true"], textarea[aria-invalid="true"], [role="combobox"][aria-invalid="true"]').forEach(el => {
    const label = labelOf(el);
    let msg = null;
    const describedBy = el.getAttribute('aria-describedby');
    if (describedBy) {
      describedBy.split(/\s+/).forEach(id => {
        const e = document.getElementById(id);
        if (e && isVisible(e)) {
          const t = clean(e.textContent);
          if (t) msg = msg ? msg + ' ' + t : t;
        }
      });
    }
    if (!msg) {
      const wrap = el.closest('.field-wrapper, .field, fieldset, [data-testid], .form-field, [class*="Field"]');
      if (wrap) {
        const e = wrap.querySelector('[class*="error" i]:not([class*="grecaptcha" i]), [role="alert"]');
        if (e && isVisible(e)) msg = clean(e.textContent);
      }
    }
    push(label, msg || 'invalid');
  });

  // 2. Visible elements whose class name contains 'error' (excluding captcha) — Greenhouse
  //    shows field errors as <p class="error">, <div class="form-error">, etc.
  document.querySelectorAll('form [class*="error" i], form [role="alert"]').forEach(el => {
    if (el.classList && Array.from(el.classList).some(c => /grecaptcha|captcha/i.test(c))) return;
    if (!isVisible(el)) return;
    const text = clean(el.textContent);
    if (!text) return;
    if (text.length > 200) return;
    let label = null;
    const wrap = el.closest('.field-wrapper, .field, fieldset, [data-testid], .form-field, [class*="Field"]');
    if (wrap) {
      const lbl = wrap.querySelector('legend, label, h2, h3, h4, [class*="label" i]');
      if (lbl) label = clean(lbl.textContent);
    }
    push(label, text);
  });

  return out;
}
"""


def find_form_errors(page: Page) -> list[dict]:
    """Return any visible field-level error messages on the form.

    Each entry is `{"field": <label or '?'>, "message": <error text>}`. Empty list means
    no validation errors are currently displayed."""
    try:
        return page.evaluate(_FIND_FORM_ERRORS_JS) or []
    except Exception:
        return []


_STEALTH_INIT_JS = r"""
// Hide the navigator.webdriver flag (most basic automation marker).
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Fake plugins / mimeTypes (headless Chrome reports empty arrays).
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
    ]
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// window.chrome is missing in vanilla puppeteer/playwright headless.
if (!window.chrome) window.chrome = {};
if (!window.chrome.runtime) window.chrome.runtime = {};

// Permissions query patch (puppeteer/playwright always returns 'denied' for notifications).
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);
}

// WebGL vendor/renderer (headless reports SwiftShader; fake real GPU).
const getParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (param) {
    if (param === 37445) return 'Intel Inc.';
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return getParam.call(this, param);
};
"""


def with_browser(headless: bool = False):
    """Launch a stealth-patched Chromium with a persistent profile so we look like a returning user."""
    pw = sync_playwright().start()
    profile_dir = (Path(__file__).resolve().parent.parent / "data" / "browser_profile")
    profile_dir.mkdir(parents=True, exist_ok=True)

    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport={"width": 1366, "height": 820},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="Asia/Kolkata",
        ignore_default_args=["--enable-automation"],
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    context.add_init_script(_STEALTH_INIT_JS)

    def page_factory() -> Page:
        return context.new_page()

    def cleanup() -> None:
        context.close()
        pw.stop()

    return pw, None, page_factory, cleanup
