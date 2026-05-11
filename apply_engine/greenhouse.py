from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeout, sync_playwright

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
    """Best-effort removal of cookie/consent/sticky banners that block form interaction.

    The JS is synchronous DOM mutation (button clicks + pointer-events:none) so no
    settle wait is needed — return immediately to keep the startup path snappy."""
    try:
        page.evaluate(_DISMISS_OVERLAYS_JS)
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
        # URL didn't change — the click likely injected a Greenhouse iframe or opened a modal.
        # Give JS time to create the iframe / render the modal.
        page.wait_for_timeout(2500)

        # First priority: a Greenhouse iframe was injected — navigate directly into it.
        gh_iframe_url = _find_greenhouse_iframe_url(page)
        if gh_iframe_url:
            page.goto(gh_iframe_url, wait_until="domcontentloaded")
            return page

        # Second: an inline/modal form appeared in the main DOM.
        try:
            page.wait_for_selector(
                "#grnhse_app form, form#application_form, form#application-form, "
                "form[action*='greenhouse.io'], input[name='first_name'], input[name='email']",
                timeout=4000,
            )
            time.sleep(0.5)
            return page
        except PlaywrightTimeout:
            return None

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


def _maybe_click_apply_button(page: Page) -> "Page | None":
    """If a non-form 'Apply Now' / 'Apply for this job' button is on the page AND the
    application form isn't already rendered inline, click the button.

    Returns a new Page if a new tab opened, the same `page` if a same-tab click happened,
    or None if no apply button was found OR the form was already on the page.

    Used as a pre-extraction step on non-Greenhouse host pages. The "form already
    inline" guard exists because some sites (e.g. Roku at weareroku.com) embed the
    full Greenhouse form below the job description AND show an Apply Now button at
    the top. Clicking it can trigger a late iframe injection that redirects us to a
    reduced post-submission view of the form. So: only click when we genuinely don't
    have a form yet."""
    # `first_name` is a Greenhouse-specific input name; matching it (or the canonical
    # Greenhouse form selectors) tells us the application form is genuinely on the page.
    # We deliberately do NOT match on `email` alone — many job pages have a talent-
    # community / newsletter widget with an email input that would falsely trip this guard.
    form_present = (
        page.locator(
            'form#application_form, #grnhse_app form, '
            'form input[name="first_name"]'
        ).count()
        > 0
    )
    if form_present:
        return None

    # Use a mix of substring and exact-text selectors:
    # - `:has-text` for distinctive phrases that won't collide with submit-button text.
    # - `:text-is("Apply")` for the bare-word case (Roku uses just "Apply"). Crucially we
    #   do NOT use `:has-text("Apply")` here — it would match "Submit Application" via
    #   substring and risk clicking the form's submit button.
    apply_btns = page.locator(
        'a:not(form a):has-text("Apply Now"), button:not(form button):has-text("Apply Now"), '
        'a:not(form a):has-text("Apply for this job"), button:not(form button):has-text("Apply for this job"), '
        'a:not(form a):text-is("Apply"), button:not(form button):text-is("Apply")'
    )
    if apply_btns.count() == 0:
        return None

    prev_url = page.url
    context = page.context
    try:
        with context.expect_page(timeout=2000) as new_page_event:
            apply_btns.first.click(timeout=5000)
        new_page = new_page_event.value
        new_page.wait_for_load_state("domcontentloaded")
        return new_page
    except PlaywrightTimeout:
        pass  # No new tab; fall through to same-tab handling
    except Exception:
        return None

    try:
        page.wait_for_url(lambda u: u != prev_url, timeout=3000)
    except PlaywrightTimeout:
        pass

    if page.url == prev_url:
        # No URL change; the click may have injected an iframe or rendered the form inline.
        page.wait_for_timeout(1500)

    return page


def open_application(page_factory, url: str) -> tuple[Page, list[Field], PageMeta]:
    page = page_factory()
    page.goto(url, wait_until="domcontentloaded")

    # If the host page is not itself a Greenhouse domain, race for the first useful
    # signal: either a Greenhouse iframe appears (embed pattern), the application form
    # renders inline, or an "Apply" button is visible. Returning on the first hit avoids
    # the multi-second tail of `networkidle` on bloated job-board hosts (analytics, chat
    # widgets, websockets often keep the network busy long after the form is interactive).
    if "greenhouse.io" not in page.url:
        try:
            page.wait_for_selector(
                'iframe[src*="greenhouse.io"], '
                "form#application_form, #grnhse_app form, "
                "input[name='first_name'], input[name='email'], "
                'a:has-text("Apply Now"), button:has-text("Apply Now"), '
                'a:has-text("Apply for this job"), button:has-text("Apply for this job")',
                timeout=5000,
            )
        except PlaywrightTimeout:
            pass

        # Click an "Apply Now" / "Apply for this job" button if present. Run this before
        # iframe lookup because some sites only inject the Greenhouse iframe after the
        # click. Safe when the form is already on the page: same-page no-op clicks just
        # cost a brief wait, and the button is restricted to elements outside any form
        # to avoid hitting a submit button.
        clicked_page = _maybe_click_apply_button(page)
        if clicked_page is not None:
            page = clicked_page

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
    _dismiss_overlays(page)

    combobox_fields = _extract_comboboxes(page)
    standard = page.evaluate(EXTRACT_JS)
    standard_fields = [Field.from_dict(d) for d in standard]

    # If no application fields found, this is likely a job-description page.
    # Follow the "Apply" link/button to reach the actual application form.
    # Allow two hops: some sites link to a Greenhouse listing page, which itself
    # requires a second "Apply for this job" click to reach the actual form.
    for _hop in range(2):
        if combobox_fields or standard_fields:
            break
        form_page = _try_follow_apply_link(page)
        if form_page is None:
            break
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

    all_fields = combobox_fields + standard_fields
    # If no location field was detected (async rendering race), try a direct fallback.
    if not any(_is_location_field(f) for f in all_fields):
        loc_fallback = _greenhouse_location_fallback(page)
        if loc_fallback:
            all_fields.append(loc_fallback)
    # Detect custom select widgets (e.g. Duolingo EEO dropdowns) missed by standard extractors.
    # Pass existing labels so any mis-identified trigger near a standard field is skipped.
    existing_labels = {f.label.strip().lower() for f in all_fields}
    all_fields += _extract_custom_selects(page, existing_labels=existing_labels)
    return page, all_fields, _extract_meta(page)


_CUSTOM_SELECT_JS = r"""
() => {
  const out = [];
  let idx = 0;
  const already = el => el.getAttribute('data-ae-key') || el.getAttribute('data-ae-skip');
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*+$/, '').replace(/\(required\)$/i, '').trim();
  const stripSelect = s => s.replace(/\s*Select\.{0,3}\s*$/i, '').replace(/\*+$/, '').trim();
  const isVis = el => {
    if (!el) return false;
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const isSelectTrigger = el =>
    /^Select(\.\.\.|)$/.test(clean(el.textContent)) &&
    !el.closest('[role="listbox"]') &&
    !el.closest('select') &&
    isVis(el) && !already(el);

  // Search within the broadest application container available.
  const root = document.querySelector('#grnhse_app, form#application_form, form') || document.body;

  // Strategy 0 (preferred): Greenhouse "ingestion form" structure. Each question lives inside
  // <DIV id="question_<id>" role="group"> with a sibling <LABEL id="question_<id>--label">.
  // Custom dropdowns wrap the actual trigger as <BUTTON aria-haspopup="listbox" aria-controls="...">.
  // Targeting the BUTTON instead of the display SPAN means click() actually opens the dropdown.
  const groups = root.querySelectorAll('[id^="question_"][role="group"]');
  for (const grp of groups) {
    if (already(grp)) continue;
    const btn = grp.querySelector('button[aria-haspopup="listbox"]');
    if (!btn || already(btn) || !isVis(btn)) continue;
    // Skip if the standard extractor already tagged a sibling input/select inside this group.
    if (grp.querySelector('[data-ae-key]:not(button[aria-haspopup="listbox"])')) continue;

    // Label: prefer the matching LABEL element by id convention, then aria-labelledby, then
    // any LABEL/legend inside the group.
    let label = null;
    const lbl = document.getElementById(grp.id + '--label');
    if (lbl) label = clean(lbl.textContent);
    if (!label) {
      const lblBy = btn.getAttribute('aria-labelledby') || grp.getAttribute('aria-labelledby');
      if (lblBy) {
        const e = document.getElementById(lblBy);
        if (e) label = clean(e.textContent);
      }
    }
    if (!label) {
      const inner = grp.querySelector('label, legend');
      if (inner) label = clean(inner.textContent);
    }
    if (!label) continue;  // unlabelled — skip rather than guess

    const key = `ae_custom_${idx++}`;
    btn.setAttribute('data-ae-key', key);
    btn.setAttribute('data-ae-combobox', '1');
    // Mark the entire group + every "Select..." display element so Strategy 1/2 don't
    // re-tag the same widget via an ancestor whose textContent collapses to "Select...".
    grp.setAttribute('data-ae-skip', '1');
    grp.querySelectorAll('span, div').forEach(s => {
      if (s === btn) return;
      const t = clean(s.textContent);
      if (/^Select(\.\.\.|)$/.test(t)) s.setAttribute('data-ae-skip', '1');
    });
    out.push({ key, label });
  }

  // Find trigger label by walking ancestors, scanning siblings before the trigger branch.
  const findLabel = (trigger) => {
    let ancestor = trigger.parentElement;
    for (let i = 0; i < 8 && ancestor; i++) {
      for (const child of ancestor.children) {
        if (child === trigger || child.contains(trigger)) break;
        const raw = clean(child.textContent);
        const txt = stripSelect(raw);
        if (txt && !/please select/i.test(txt) && txt.length > 3 && txt.length < 300) return txt;
      }
      ancestor = ancestor.parentElement;
    }
    return null;
  };

  // Strategy 1: "Please select the one that applies to you" proximity search.
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const seenLbl = new Set();
  while (walker.nextNode()) {
    const t = clean(walker.currentNode.textContent);
    if (!/please select.*applies to you/i.test(t)) continue;
    const lbl = walker.currentNode.parentElement;
    if (!lbl || seenLbl.has(lbl) || already(lbl)) continue;
    seenLbl.add(lbl);

    let trigger = null, question = null, scope = lbl;
    for (let depth = 0; depth < 6 && scope && !trigger; depth++) {
      const container = scope.parentElement;
      if (!container) break;

      // Question text: scan siblings BEFORE scope (in both directions)
      if (!question) {
        for (let el = scope.previousElementSibling; el; el = el.previousElementSibling) {
          const txt = stripSelect(clean(el.textContent));
          if (txt && !/please select/i.test(txt) && txt.length > 3 && txt.length < 300) { question = txt; break; }
        }
      }

      // Trigger: scan siblings AFTER scope
      for (let el = scope.nextElementSibling; el && !trigger; el = el.nextElementSibling) {
        const cands = [el, ...Array.from(el.querySelectorAll('*'))].filter(isSelectTrigger);
        if (cands.length) trigger = cands[cands.length - 1];
      }
      // Also scan inside siblings BEFORE scope (trigger may be inside the question container)
      if (!trigger) {
        for (let el = scope.previousElementSibling; el && !trigger; el = el.previousElementSibling) {
          const cands = Array.from(el.querySelectorAll('*')).filter(isSelectTrigger);
          if (cands.length) trigger = cands[cands.length - 1];
        }
      }

      scope = container;
    }
    if (!trigger || already(trigger)) continue;
    if (!question) question = findLabel(trigger);

    const key = `ae_custom_${idx++}`;
    trigger.setAttribute('data-ae-key', key);
    trigger.setAttribute('data-ae-combobox', '1');
    out.push({ key, label: question || 'Please select the one that applies to you' });
  }

  // Strategy 2: catch remaining untagged "Select..." triggers anywhere in the form.
  // Handles EEO dropdowns whose labels don't use "please select" phrasing.
  const allEls = Array.from(root.querySelectorAll('button, div[class], span[class], li'));
  for (const el of allEls) {
    if (!isSelectTrigger(el)) continue;
    // Only keep leaves: element should have no child elements that are also triggers
    if (Array.from(el.querySelectorAll('*')).some(isSelectTrigger)) continue;
    const question = findLabel(el);
    if (!question) continue;  // skip unlabelled triggers (likely irrelevant UI widgets)
    const key = `ae_custom_${idx++}`;
    el.setAttribute('data-ae-key', key);
    el.setAttribute('data-ae-combobox', '1');
    out.push({ key, label: question });
  }

  return out;
}
"""


def _extract_custom_selects(page: Page, existing_labels: set[str] | None = None) -> list[Field]:
    """Detect custom non-native select widgets (e.g. Duolingo EEO dropdowns) in the form.

    existing_labels: lowercase label strings already handled by standard extractors.
    Any trigger whose detected label matches one of these is skipped *before* the
    discovery click, preventing spurious scroll/click cycles on mis-identified elements.
    """
    raw = page.evaluate(_CUSTOM_SELECT_JS)
    fields: list[Field] = []
    if not raw:
        return fields

    # Save scroll position so the page doesn't end up parked wherever the last trigger lived
    # — minimises perceived "scrolling up and down" churn for the user.
    try:
        scroll_y = page.evaluate("() => window.scrollY")
    except Exception:
        scroll_y = None

    for item in raw:
        key = item["key"]
        label = item.get("label") or "Please select the one that applies to you"

        # Skip before clicking if this label belongs to an already-handled field.
        # This prevents false positives (e.g. a "Select..." trigger near "First Name"
        # being misidentified as a custom dropdown) from causing unnecessary scroll/click.
        if existing_labels and label.strip().lower() in existing_labels:
            try:
                page.evaluate(
                    "key => { const e = document.querySelector(`[data-ae-key=\"${key}\"]`);"
                    " if (e) { e.removeAttribute('data-ae-key'); e.removeAttribute('data-ae-combobox'); } }",
                    key,
                )
            except Exception:
                pass
            continue

        sel = f'[data-ae-key="{key}"]'
        options: list[str] = []
        try:
            # Short timeout: a misidentified trigger should fail fast, not block for 30s.
            loc = page.locator(sel).first
            loc.click(timeout=4000)
            page.wait_for_timeout(350)
            options = page.evaluate(_READ_DROPDOWN_OPTIONS_JS)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        except Exception:
            options = []
            # Best-effort dismiss in case the dropdown is half-open.
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        if not options:
            # Un-tag so a later conditional pass can re-check once dependent fields are filled.
            try:
                page.locator(sel).first.evaluate(
                    "el => { el.removeAttribute('data-ae-key'); el.removeAttribute('data-ae-combobox'); }"
                )
            except Exception:
                pass
            continue

        searchable = len(options) > SEARCHABLE_THRESHOLD
        fields.append(Field(
            key=key,
            type="searchable_select" if searchable else "select",
            label=label,
            name=None,
            required=True,
            options=None if searchable else options,
            max_length=None,
        ))

    if scroll_y is not None:
        try:
            page.evaluate("y => window.scrollTo(0, y)", scroll_y)
        except Exception:
            pass

    return fields


def _extract_comboboxes(page: Page) -> list[Field]:
    raw = page.evaluate(COMBOBOX_TAG_JS)
    fields: list[Field] = []
    if not raw:
        return fields

    try:
        scroll_y = page.evaluate("() => window.scrollY")
    except Exception:
        scroll_y = None

    for box in raw:
        sel = f'[data-ae-key="{box["key"]}"]'
        options: list[str] = []
        try:
            page.locator(sel).first.click(timeout=4000)
            page.wait_for_timeout(250)
            options = _read_visible_options(page)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        except Exception:
            options = []
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

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

    if scroll_y is not None:
        try:
            page.evaluate("y => window.scrollTo(0, y)", scroll_y)
        except Exception:
            pass

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


def _sort_fields_by_position(page: Page, fields: list[Field]) -> list[Field]:
    """Sort extracted fields by their tagged element's absolute Y-position.

    The extractor pipeline runs combobox detection (``_extract_comboboxes``) and
    standard-field detection (``EXTRACT_JS``) in two passes, and on a Greenhouse
    form like PhonePe's the two groups are interleaved on the page. Without
    re-sorting, the ``form_order`` block sent to the AI groups all comboboxes at
    the top and all standard inputs at the bottom — so an Education year input
    (number, standard) reads as "next to LinkedIn" instead of "next to School/Degree",
    and the AI has no way to assign it to the right section.

    Fields whose element can't be measured (e.g. tag was stripped during empty-options
    recovery) sink to the end, preserving their original relative order. Returns a
    new list — original order is preserved if measurement fails completely."""
    keys = [f.key for f in fields]
    try:
        positions = page.evaluate(
            r"""(keys) => keys.map(k => {
                const el = document.querySelector(`[data-ae-key="${k.replace(/"/g, '\\\"')}"]`);
                if (!el) return Number.MAX_SAFE_INTEGER;
                const r = el.getBoundingClientRect();
                return r.top + window.scrollY;
            })""",
            keys,
        )
    except Exception:
        return list(fields)
    if not isinstance(positions, list) or len(positions) != len(fields):
        return list(fields)
    indexed = list(enumerate(zip(positions, fields)))
    indexed.sort(key=lambda t: (t[1][0], t[0]))
    return [f for _, (_, f) in indexed]


def _is_truthy(value: str) -> bool:
    """True for affirmative scalar/JSON-list values.

    Accepts plain strings ("Yes"/"true"/"1"/"on") and JSON-encoded single-item
    lists like ``'["Yes"]'`` (which is what the AI / older stored answers emit
    when a "Current role?" checkbox was treated as a multiselect group)."""
    s = value.strip()
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list) and parsed:
                s = str(parsed[0])
        except json.JSONDecodeError:
            pass
    return s.strip().lower() in {"yes", "true", "1", "on"}


def _force_check(page: Page, loc: Locator, checked: bool) -> None:
    """Check or uncheck a checkbox/radio with progressively more forceful strategies,
    verifying the DOM state after each attempt before trying the next."""

    def _ok() -> bool:
        try:
            return loc.is_checked() == checked
        except Exception:
            return False

    try:
        if checked:
            loc.check(timeout=5000)
        else:
            loc.uncheck(timeout=5000)
        if _ok():
            return
    except Exception:
        pass

    _dismiss_overlays(page)
    page.wait_for_timeout(200)

    # Keyboard Space bypasses pointer-events CSS — guard prevents toggling away from desired state
    try:
        loc.scroll_into_view_if_needed()
        loc.focus()
        if not _ok():
            loc.press("Space")
        if _ok():
            return
    except Exception:
        pass

    # CDP force-click bypasses actionability checks
    try:
        loc.click(force=True, timeout=3000)
        if _ok():
            return
    except Exception:
        pass

    # Clicking the <label> can reach elements the input itself can't
    try:
        input_id = loc.get_attribute("id")
        if input_id:
            label = page.locator(f'label[for="{input_id}"]')
            if label.count() > 0:
                label.first.click(force=True, timeout=3000)
                if _ok():
                    return
    except Exception:
        pass

    # Native prototype setter bypasses React's instance-property override so the
    # change event triggers React's synthetic event pipeline via document delegation.
    checked_js = "true" if checked else "false"
    loc.evaluate(
        f"el => {{"
        f"const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked').set;"
        f"s.call(el, {checked_js});"
        f"el.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));"
        f"el.dispatchEvent(new Event('input', {{bubbles: true}}));"
        f"el.dispatchEvent(new Event('change', {{bubbles: true}}));"
        f"}}"
    )


_EXTRACT_JOB_DESC_JS = r"""
() => {
  const selectors = [
    '.job-description', '#job-description', '[class*="job-description"]',
    '[class*="jobDescription"]', '[id*="job-description"]',
    '#content', '.content', 'article', 'main section',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) {
      const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if (txt.length > 100) return txt.slice(0, 5000);
    }
  }
  return ((document.body.innerText || '').replace(/\s+/g, ' ').trim()).slice(0, 5000);
}
"""


def extract_job_description(page: Page) -> str:
    """Extract the job description text from the current page."""
    try:
        return page.evaluate(_EXTRACT_JOB_DESC_JS) or ""
    except Exception:
        return ""


def _is_cover_letter_field(field: "Field") -> bool:
    return bool(re.search(r"cover.?letter", field.label, re.I))


def fill_field(page: Page, field: Field, value: str, resume_path: Path | None = None, cover_letter_path: Path | None = None) -> str | None:
    selector = f'[data-ae-key="{field.key}"]'

    # An empty value on a non-file field means we have no answer to commit. Skip
    # interaction entirely. Without this, combobox helpers would tag the first
    # available option (since target="" matches everything as a substring) and
    # commit a wrong value (e.g. "January" for an empty End-date-month), and
    # text helpers would still call loc.click()+fill("") which on a *disabled*
    # element (e.g. End-date-year auto-disabled when "Current role" is checked)
    # blocks for the full Playwright timeout (30s) before erroring.
    if field.type != "file" and not value.strip():
        return "skipped"

    # An element flagged disabled (or aria-disabled) means the form has decided
    # this field shouldn't be filled — typically a date field auto-disabled by a
    # neighbouring "currently work here" checkbox. Skip rather than wait through
    # Playwright's actionability timeout.
    if field.type != "file":
        try:
            disabled = page.locator(selector).first.evaluate(
                "el => !!(el.disabled || el.getAttribute('aria-disabled') === 'true')"
            )
            if disabled:
                return "skipped"
        except Exception:
            pass

    if field.type == "file":
        # "File upload" is the fallback label assigned when label extraction fails.
        # Treat it as a resume slot if resume not yet placed (caller sets resume_path=None after first upload).
        _is_resume_label = bool(re.search(r"resume|cv\b|curriculum", field.label, re.I))
        _is_generic_label = field.label.strip().lower() == "file upload"
        if resume_path and (_is_resume_label or _is_generic_label):
            page.locator(selector).first.set_input_files(str(resume_path))
            return "uploaded"
        if cover_letter_path and _is_cover_letter_field(field):
            page.locator(selector).first.set_input_files(str(cover_letter_path))
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
        # leaves the form value empty until you pick a suggestion. If no suggestion
        # appears, fall back to plain typing (the field is probably not a typeahead).
        if _is_location_field(field) and value:
            if _fill_location(page, loc, value):
                return
            loc.fill("")  # _fill_location may have left partial text
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
        if _is_location_field(field) and value:
            loc = page.locator(selector).first
            try:
                loc.click(timeout=5000)
            except Exception:
                _dismiss_overlays(page)
                loc.click(force=True)
            loc.fill("")
            _fill_location(page, loc, value)
            return
        fallbacks = _INDUSTRY_FALLBACKS if _is_industry_field(field) else None
        _type_and_pick(page, selector, value, fallbacks=fallbacks)
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
                _force_check(page, box, True)
            elif not should_check and box.is_checked():
                _force_check(page, box, False)
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
        _force_check(page, target.first, True)
        return

    if field.type == "checkbox":
        loc = page.locator(selector).first
        truthy = _is_truthy(value)
        if truthy and not loc.is_checked():
            _force_check(page, loc, True)
        elif not truthy and loc.is_checked():
            _force_check(page, loc, False)
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


# Tags the best-matching visible dropdown option with data-ae-click-target="1" so
# Playwright can click it (dispatching proper synthetic events React handles).
# Tries strategies in priority order: ARIA listbox → ARIA roles → li[tabindex≥0] →
# absolutely-positioned containers (portals) → any visible ul>li list.
_TAG_OPTION_JS = r"""
(target) => {
    const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
    const isVis = el => {
        if (!el) return false;
        const cs = getComputedStyle(el);
        if (cs.display==='none'||cs.visibility==='hidden') return false;
        return el.offsetParent!==null || cs.position==='fixed';
    };
    const t = norm(target);
    const rank = el => {
        const et=norm(el.textContent);
        return et===t ? 3 : et.startsWith(t) ? 2 : et.includes(t) ? 1 : 0;
    };
    const best = els => els.map(el=>[el,rank(el)]).filter(([,r])=>r>0).sort((a,b)=>b[1]-a[1])[0]?.[0]||null;
    const tag = el => {
        document.querySelectorAll('[data-ae-click-target]').forEach(e=>e.removeAttribute('data-ae-click-target'));
        el.setAttribute('data-ae-click-target','1');
        return true;
    };
    // S1: ARIA listbox → option
    for (const lb of [...document.querySelectorAll('[role="listbox"]')].filter(isVis)) {
        const m=best([...lb.querySelectorAll('[role="option"]')].filter(isVis));
        if(m) return tag(m);
    }
    // S2: Any ARIA option/menu role
    const m2=best([...document.querySelectorAll('[role="option"],[role="menuitem"],[role="menuitemradio"]')].filter(isVis));
    if(m2) return tag(m2);
    // S3: li with non-negative tabindex (react-select, downshift, slim-select)
    const m3=best([...document.querySelectorAll('li[tabindex]')].filter(el=>isVis(el)&&el.tabIndex>=0));
    if(m3) return tag(m3);
    // S4: Absolutely/fixed-positioned containers — dropdown portals, custom widgets
    const dropsels='ul,ol,[class*="dropdown"],[class*="select__"],[class*="menu__"],[class*="-options"],[class*="-list"]';
    for (const c of [...document.querySelectorAll(dropsels)].filter(el=>{
        if(!isVis(el)) return false;
        const p=getComputedStyle(el).position;
        return p==='absolute'||p==='fixed';
    })){
        const items=[...c.querySelectorAll('div,li,span')].filter(
            el=>isVis(el)&&el.childElementCount===0&&norm(el.textContent).length>0&&norm(el.textContent).length<200
        );
        const m=best(items);
        if(m) return tag(m);
    }
    // S5: Any visible ul with ≥2 direct li children (catches plain custom dropdowns)
    for (const ul of [...document.querySelectorAll('ul')].filter(isVis)){
        const items=[...ul.querySelectorAll(':scope>li')].filter(isVis);
        if(items.length>=2){const m=best(items);if(m) return tag(m);}
    }
    return false;
}
"""

# Read all visible dropdown option texts — comprehensive version used for custom EEO selects.
# Falls through strategies until options are found, same priority order as _TAG_OPTION_JS.
_READ_DROPDOWN_OPTIONS_JS = r"""
() => {
    const isVis = el => el.offsetParent!==null && getComputedStyle(el).display!=='none';
    const clean = s => (s||'').replace(/\s+/g,' ').trim();
    const add = (items,out) => items.filter(isVis).forEach(o=>{const t=clean(o.textContent);if(t&&t.length<200) out.add(t);});
    const out=new Set();
    [...document.querySelectorAll('[role="listbox"]')].filter(isVis).forEach(lb=>add([...lb.querySelectorAll('[role="option"]')],out));
    if(out.size) return [...out];
    add([...document.querySelectorAll('[role="option"],[role="menuitem"],[role="menuitemradio"]')],out);
    if(out.size) return [...out];
    add([...document.querySelectorAll('li[tabindex]')].filter(el=>el.tabIndex>=0),out);
    if(out.size) return [...out];
    const dropsels='ul,ol,[class*="dropdown"],[class*="select__"],[class*="menu__"],[class*="-options"],[class*="-list"]';
    for (const c of [...document.querySelectorAll(dropsels)].filter(el=>{
        if(!isVis(el)) return false;
        const p=getComputedStyle(el).position;
        return p==='absolute'||p==='fixed';
    })){
        add([...c.querySelectorAll('div,li,span')].filter(el=>el.childElementCount===0),out);
        if(out.size) return [...out];
    }
    for (const ul of [...document.querySelectorAll('ul')].filter(isVis)){
        const items=[...ul.querySelectorAll(':scope>li')].filter(isVis);
        if(items.length>=2){ add(items,out); if(out.size) return [...out]; }
    }
    return [...out];
}
"""


def _click_visible_option(page: Page, value: str) -> bool:
    """Tag the best-matching visible dropdown option and click it via Playwright.

    Uses _TAG_OPTION_JS to find the option across any dropdown structure (ARIA listbox,
    custom portals, plain ul/li) and tags it with data-ae-click-target. Playwright then
    clicks the tagged element, dispatching proper synthetic events that React handles.
    """
    tagged = page.evaluate(_TAG_OPTION_JS, value.strip().lower())
    if not tagged:
        return False
    tgt = page.locator('[data-ae-click-target="1"]')
    try:
        if tgt.count() > 0:
            tgt.first.scroll_into_view_if_needed()
            tgt.first.click(timeout=2000)
            return True
    except Exception:
        pass
    finally:
        try:
            page.evaluate("() => document.querySelectorAll('[data-ae-click-target]').forEach(e=>e.removeAttribute('data-ae-click-target'))")
        except Exception:
            pass
    return False


_PLACEHOLDER_RE = re.compile(
    r"^(select|please\s*select|choose|pick|none\s+selected)\b|^-{2,}", re.I
)


def _trigger_display_text(page: Page, selector: str) -> str:
    """Return the visible text/value of a combobox trigger so callers can verify a
    selection actually committed.

    react-select's trigger is a hidden ``<input role="combobox">`` whose ``.value``
    is always empty — the selected option is rendered in a sibling
    ``.select__single-value`` div inside the ``.select__control`` wrapper. So for
    INPUT triggers we look at the wrapper's textContent (excluding the placeholder
    "Select..." span) before falling back to ``el.value``. Buttons/divs use their
    own textContent. Empty string on any failure."""
    try:
        return page.locator(selector).first.evaluate(
            r"""(el) => {
                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                    // react-select v5: walk up to .select__control (or any *-control wrapper)
                    // and read its visible single-value / multi-value text.
                    let cur = el;
                    for (let i = 0; i < 6 && cur; i++) {
                        if (cur.classList && [...cur.classList].some(c => /control$/.test(c))) {
                            const sv = cur.querySelector('[class*="single-value"], [class*="multi-value__label"]');
                            if (sv) {
                                const t = (sv.textContent || '').replace(/\s+/g, ' ').trim();
                                if (t) return t;
                            }
                            // No single-value div = nothing selected. Don't fall back to control text
                            // (which would include the "Select..." placeholder).
                            return (el.value || '').trim();
                        }
                        cur = cur.parentElement;
                    }
                    return (el.value || '').trim();
                }
                return (el.textContent || '').replace(/\s+/g, ' ').trim();
            }"""
        ) or ""
    except Exception:
        return ""


def _trigger_looks_unselected(page: Page, selector: str) -> bool:
    """True if the trigger still shows a placeholder (e.g. 'Select...') after a
    pick attempt. Used to detect silent fill failures where _click_visible_option
    fired a click but React/the form didn't commit the selection."""
    text = _trigger_display_text(page, selector)
    if not text:
        return True
    return bool(_PLACEHOLDER_RE.match(text))


def _open_combobox_trigger(page: Page, selector: str) -> None:
    """Click a combobox trigger to open its dropdown, robustly.

    react-select's hidden ``<input role="combobox">`` is only ~3px wide. After a
    previous text field is filled (which leaves focus on that input), Playwright's
    direct click on the next combobox input may not reach the trigger that React
    listens to — the dropdown stays closed (``aria-expanded`` remains ``false``)
    and the subsequent option-tagging finds no visible listbox.

    Blurring the currently-focused element first, then clicking, fixes it. We
    also verify the dropdown actually opened by checking ``aria-expanded`` or a
    visible listbox, retrying once with a forced click on the parent ``select__control``
    wrapper if the trigger is too small to hit reliably."""
    try:
        page.evaluate("() => document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    except Exception:
        pass
    page.wait_for_timeout(300)  # let the blur event + React state flush before the next click
    loc = page.locator(selector).first
    loc.click()
    page.wait_for_timeout(400)
    if _combobox_is_open(page, selector):
        return
    # Fallback: click the react-select wrapper (.select__control / parent up to 3 levels).
    try:
        loc.evaluate(
            r"""el => {
                let cur = el;
                for (let i = 0; i < 4 && cur; i++) {
                    if (cur.classList && [...cur.classList].some(c => /control$/.test(c))) {
                        cur.click();
                        return;
                    }
                    cur = cur.parentElement;
                }
                // Last resort: click two levels up (typical react-select wrapper depth).
                if (el.parentElement?.parentElement) el.parentElement.parentElement.click();
            }"""
        )
    except Exception:
        pass
    page.wait_for_timeout(300)


def _combobox_is_open(page: Page, selector: str) -> bool:
    """True if the trigger reports ``aria-expanded="true"`` OR there's a visible
    listbox other than the international-tel-input country listbox."""
    try:
        expanded = page.locator(selector).first.get_attribute("aria-expanded")
        if expanded == "true":
            return True
    except Exception:
        pass
    try:
        return bool(page.evaluate(
            r"""() => {
                const lbs = [...document.querySelectorAll('[role="listbox"]')];
                return lbs.some(lb => {
                    if ((lb.id || '').includes('iti-')) return false;
                    const cs = getComputedStyle(lb);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                    if (lb.offsetParent === null && cs.position !== 'fixed') return false;
                    return lb.querySelectorAll('[role="option"]').length > 0;
                });
            }"""
        ))
    except Exception:
        return False


def _pick_combobox_option(page: Page, selector: str, value: str) -> None:
    _open_combobox_trigger(page, selector)
    page.wait_for_timeout(200)  # allow dropdown animation / React state settle
    if _click_visible_option(page, value):
        page.wait_for_timeout(200)
        if not _trigger_looks_unselected(page, selector):
            return
    # Value didn't pick (or click didn't commit) — try "Other" as a fallback.
    if _click_visible_option(page, "Other"):
        page.wait_for_timeout(200)
        if not _trigger_looks_unselected(page, selector):
            return
    page.keyboard.press("Escape")
    raise ValueError(f"No combobox option matches {value!r}")


def _wait_for_dropdown_options(page: Page, timeout_ms: int) -> int:
    """Poll the visible (non-iti) listboxes for at least one ``[role="option"]`` to
    appear, returning the option count when it does (or 0 on timeout). Read-only —
    does not touch the dropdown state."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        n = page.evaluate(r"""() => {
            const lbs = [...document.querySelectorAll('[role="listbox"]')].filter(lb => {
                if ((lb.id || '').includes('iti-')) return false;
                const cs = getComputedStyle(lb);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                if (lb.offsetParent === null && cs.position !== 'fixed') return false;
                return true;
            });
            return lbs.reduce((s, lb) => s + lb.querySelectorAll('[role="option"]').length, 0);
        }""")
        if n > 0:
            return n
        page.wait_for_timeout(150)
    return 0


def _type_and_pick(
    page: Page,
    selector: str,
    value: str,
    fallbacks: list[str] | None = None,
) -> None:
    """For long-list comboboxes (countries / city autocompletes): focus, type, wait
    for the dropdown to populate (server-side filtering races the click), then click
    the best match.

    We wait *passively* for options to appear instead of poll-clicking — repeatedly
    calling ``_click_visible_option`` against an empty dropdown nudges react-select's
    internal state in ways that break a later "Other" fallback (the dropdown stops
    re-populating on subsequent typing). One click attempt per typed value is enough
    when we've already confirmed options are present.

    ``fallbacks``: ordered alternates to try if the primary value doesn't match (e.g.
    industry synonyms). All fallbacks are tried before the generic "Other" fallback."""
    _open_combobox_trigger(page, selector)
    loc = page.locator(selector).first

    def _try(candidate: str, wait_ms: int) -> bool:
        loc.fill("")
        page.wait_for_timeout(200)
        if not _combobox_is_open(page, selector):
            _open_combobox_trigger(page, selector)
        loc.press_sequentially(candidate, delay=60)
        if _wait_for_dropdown_options(page, wait_ms) > 0:
            if _click_visible_option(page, candidate):
                page.wait_for_timeout(200)
                if not _trigger_looks_unselected(page, selector):
                    return True
        return False

    if _try(value, 3000):
        return
    for fb in (fallbacks or []):
        if _try(fb, 2000):
            return
    if _try("Other", 2000):
        return
    fb_msg = f" or fallbacks {fallbacks!r}" if fallbacks else ""
    raise ValueError(f"No combobox option matched {value!r}{fb_msg} after typing")


# Greenhouse "Current Industry" pickers list canonical industry names which often
# don't match the verbose AI suggestion ("Information Technology and Services").
# Try common synonyms before falling back to Financial Services (LSEG's industry).
_INDUSTRY_FALLBACKS: list[str] = [
    "Information Technology",
    "IT Services",
    "Software",
    "Computer Software",
    "Technology",
    "IT",
    "Financial Services",
    "Finance",
]


def _is_industry_field(field: "Field") -> bool:
    return bool(re.search(r"\bindustry\b", field.label, re.I))


def _is_location_field(field: "Field") -> bool:
    label = field.label.strip()
    if not re.search(r"\b(location|city|town)\b", label, re.I):
        return False
    # Skip custom questions that contain a colon followed by content, e.g.
    # "Preferred location: Noida/Bengaluru" — these are plain-text questions, not
    # autocomplete typeaheads, and routing them through _fill_location wipes the value.
    if re.search(r"\b(location|city|town)\b\s*:", label, re.I):
        return False
    return True


def _greenhouse_location_fallback(page: Page) -> "Field | None":
    """Detect Greenhouse's location input when standard extractors miss it.

    Handles two patterns:
    - Classic: input[name="job_application[location]"] (text type)
    - Modern:  input[type="search"][aria-haspopup="listbox"] near a location label
    """
    result = page.evaluate(r"""() => {
        const already = (el) => el.getAttribute('data-ae-key') || el.getAttribute('data-ae-skip');
        const clean = (el) => (el.textContent || '').replace(/\s+/g, ' ')
            .replace(/\*+$/, '').replace(/\(required\)$/i, '').trim();
        const locationRe = /\b(location|city|town)\b/i;

        // Pattern 1: classic Greenhouse location input (text type, known name/id)
        const classic = Array.from(document.querySelectorAll(
            'input[name="job_application[location]"], '
            + 'input[id="job_application_location"], '
            + 'input[data-testid*="location" i], '
            + 'input[placeholder*="city" i], '
            + 'input[placeholder*="location" i]'
        )).filter(el => !already(el) && el.type !== 'hidden');
        for (const el of classic) {
            const key = 'ae_gh_location';
            el.setAttribute('data-ae-key', key);
            let label = 'Location (city)';
            if (el.id) {
                try {
                    const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (lbl) { const t = clean(lbl); if (t) label = t; }
                } catch (e) {}
            }
            if (label === 'Location (city)') {
                const aria = el.getAttribute('aria-label');
                if (aria) label = aria.trim();
            }
            return { key, label, required: el.required, inputType: el.type };
        }

        // Pattern 2: visible search inputs near a location label (Duolingo-style combobox).
        // The widget uses two sibling inputs: one hidden trigger (aria-haspopup) and one
        // visible input (no aria-haspopup, tabindex≥0) that the user actually types into.
        const searchInputs = Array.from(document.querySelectorAll('input[type="search"]'))
            .filter(el =>
                !already(el) &&
                !el.getAttribute('aria-haspopup') &&
                getComputedStyle(el).visibility === 'visible'
            );
        for (const el of searchInputs) {
            let label = null;
            let ancestor = el.parentElement;
            for (let i = 0; i < 8; i++) {
                if (!ancestor) break;
                const lbl = ancestor.querySelector(':scope > label');
                if (lbl) { label = clean(lbl); break; }
                ancestor = ancestor.parentElement;
            }
            if (!label) {
                const aria = el.getAttribute('aria-label') || '';
                if (aria) label = aria.trim();
            }
            if (label && locationRe.test(label)) {
                const key = 'ae_gh_location';
                el.setAttribute('data-ae-key', key);
                return { key, label, required: true, inputType: 'search' };
            }
        }
        return null;
    }""")
    if not result:
        return None
    return Field(
        key=result["key"],
        type="text",
        label=result.get("label") or "Location (city)",
        name=None,
        required=bool(result.get("required", True)),
        options=None,
        max_length=None,
    )


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


_AUTOCOMPLETE_PICK_FIRST_JS = r"""
() => {
  const isVisible = (el) => {
    if (!el || el.offsetParent === null) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  const selectors = [
    '[role="listbox"] [role="option"]', '[role="option"]',
    'ul[class*="suggest" i] li', 'ul[class*="autocomplete" i] li',
    'ul[class*="location" i] li', '[class*="suggestions" i] li',
    '[class*="dropdown" i] [class*="option" i]', '[class*="dropdown" i] [class*="item" i]',
    '[class*="menu" i] [class*="item" i]', '.pac-container .pac-item',
    '.geosuggest__item', '[id*="downshift" i] li',
  ];
  const candidates = Array.from(document.querySelectorAll(selectors.join(', ')))
    .filter(isVisible)
    .filter(el => (el.textContent || '').replace(/\s+/g, ' ').trim().length > 0);
  if (!candidates.length) return null;
  const pick = candidates[0];
  pick.scrollIntoView({ block: 'center' });
  pick.click();
  return (pick.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
}
"""


def _fill_location(page: Page, loc, value: str) -> bool:
    """Type a location and commit a suggestion. Polls the dropdown for up to ~4s
    because Greenhouse debounces the autocomplete request. Returns True if a suggestion
    was committed."""

    def _try_type_and_pick(search: str) -> str | None:
        loc.fill("")
        loc.press_sequentially(search, delay=80)
        deadline = time.time() + 3.5
        while time.time() < deadline:
            # Prefer Playwright's pointer-event click (React-compatible) over DOM .click().
            # _click_visible_option uses _TAG_OPTION_JS which covers ARIA listboxes,
            # custom portals, plain ul/li, and common autocomplete containers.
            if _click_visible_option(page, value):
                return value
            # Fallback: broader selectors (geosuggest, ul.suggest, etc.)
            try:
                result = page.evaluate(_AUTOCOMPLETE_PICK_JS, value)
            except Exception:
                result = None
            if result:
                return result
            page.wait_for_timeout(200)
        # One extra second, then take the first available suggestion
        page.wait_for_timeout(1000)
        try:
            first_text = page.evaluate(_AUTOCOMPLETE_PICK_FIRST_JS)
        except Exception:
            first_text = None
        if first_text:
            # Try Playwright click on the matched text first; DOM click already fired above.
            _click_visible_option(page, first_text)
            return first_text
        return None

    # Pass 1: full city name
    picked = _try_type_and_pick(value)

    # Pass 2: shorter prefix (handles alternate spellings / slower APIs)
    if not picked and len(value) >= 4:
        picked = _try_type_and_pick(value[:4])

    if not picked:
        # Clear — leaving unselected text causes form validation errors
        loc.fill("")
        return False
    # Tab off so the React component locks in the selection before we move on.
    loc.press("Tab")
    page.wait_for_timeout(300)
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
    if btn_loc.count() == 0:
        raise ValueError("No submit button found on page")
    # Pick by text affinity, NOT raw DOM order. A page can carry multiple
    # `<button type="submit">` elements: a job-favourites "Favorited" button, the
    # actual application's "Submit Application" button, and a "Submit" button on a
    # talent-community widget. The original "first visible in DOM order" heuristic
    # silently clicked the wrong one. Three-pass ranking:
    #   1. Visible button whose text contains "submit application" — canonical Greenhouse label.
    #   2. Visible button whose text contains "submit" (and isn't the favourites button).
    #   3. Fallback: first visible candidate of any kind.
    n = min(btn_loc.count(), 10)

    def _btn_text(c) -> str:
        try:
            return c.evaluate(
                "el => (el.textContent || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase()"
            )
        except Exception:
            return ""

    btn = None
    for i in range(n):
        c = btn_loc.nth(i)
        try:
            if not c.is_visible():
                continue
        except Exception:
            continue
        if "submit application" in _btn_text(c):
            btn = c
            break
    if btn is None:
        for i in range(n):
            c = btn_loc.nth(i)
            try:
                if not c.is_visible():
                    continue
            except Exception:
                continue
            text = _btn_text(c)
            if "submit" in text and "favorit" not in text and not text.startswith("save"):
                btn = c
                break
    if btn is None:
        for i in range(n):
            c = btn_loc.nth(i)
            try:
                if c.is_visible():
                    btn = c
                    break
            except Exception:
                continue
    if btn is None:
        raise ValueError("No submit button found on page")
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
  const messagesByField = new Map();  // message → bool (any real-label hit?)
  const push = (field, message) => {
    const m = clean(message);
    if (!m) return;
    if (m.length > 200) return;
    const key = (field || '') + '||' + m;
    if (seen.has(key)) return;
    seen.add(key);
    // If we've already attributed this message to a real label, drop the "?" copy.
    if (!field && messagesByField.get(m)) return;
    if (field) messagesByField.set(m, true);
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
    // Greenhouse adds an "error" class to the <label> itself when validation fails;
    // its text content is the field label, not an error message — skip it.
    const tag = el.tagName;
    if (tag === 'LABEL' || tag === 'LEGEND') return;
    // Skip elements that wrap labels (the label's text bubbles up via textContent).
    if (el.querySelector && el.querySelector('label, legend')) return;
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
    // Skip if the "error" text is just the field label repeated (Greenhouse wrappers
    // sometimes have an .error class whose textContent equals the label).
    if (label && clean(text).toLowerCase() === clean(label).toLowerCase()) return;
    push(label, text);
  });

  return out;
}
"""


def extract_new_standard_fields(page: Page, existing_labels: set[str] | None = None) -> list[Field]:
    """Re-extract standard fields that became visible since the last extraction pass.

    EXTRACT_JS already skips elements tagged with data-ae-key, so this safely
    returns only newly-visible native inputs/selects/textareas. Useful for catching
    conditional fields that appear after earlier form answers are submitted."""
    standard = page.evaluate(EXTRACT_JS)
    new_fields = [Field.from_dict(d) for d in standard]
    if existing_labels:
        new_fields = [f for f in new_fields if f.label.strip().lower() not in existing_labels]
    return new_fields


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
