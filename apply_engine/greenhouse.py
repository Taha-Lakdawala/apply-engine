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

  const form = document.querySelector('form#application_form, form#application-form, form[action*="apply"], form');
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


# How many options before we treat a combobox as a free-text searchable field
# (eg country lists with 200+ entries — too many to enumerate to the model).
SEARCHABLE_THRESHOLD = 25


def open_application(page_factory, url: str) -> tuple[Page, list[Field], PageMeta]:
    page = page_factory()
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("form input, form select, form textarea, form [role='combobox']", timeout=15000)
    except PlaywrightTimeout:
        pass
    time.sleep(0.6)

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
    return PageMeta(title=title, company=company)


def fill_field(page: Page, field: Field, value: str, resume_path: Path | None = None) -> str | None:
    selector = f'[data-ae-key="{field.key}"]'

    if field.type == "file":
        if resume_path and re.search(r"resume|cv\b|curriculum", field.label, re.I):
            page.locator(selector).first.set_input_files(str(resume_path))
            return "uploaded"
        return "skipped"

    if field.type in {"text", "email", "phone", "url", "number", "date", "textarea"}:
        loc = page.locator(selector).first
        loc.click()
        loc.fill("")  # clear pre-filled value
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
    """Click an option matching value within a visible listbox. Returns True if clicked."""
    target_lower = value.strip().lower()
    return page.evaluate(
        """(target) => {
            const visible = Array.from(document.querySelectorAll('[role="listbox"]'))
                .filter(lb => lb.offsetParent !== null);
            for (const lb of visible) {
                const opts = Array.from(lb.querySelectorAll('[role="option"]'));
                let match = opts.find(o => (o.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase() === target);
                if (!match) match = opts.find(o => (o.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase().includes(target));
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
        page.keyboard.press("Escape")
        raise ValueError(f"No combobox option matches {value!r}")


def _type_and_pick(page: Page, selector: str, value: str) -> None:
    """For long-list comboboxes (countries etc): focus, type, pick best match."""
    loc = page.locator(selector).first
    loc.click()
    loc.fill("")
    loc.type(value, delay=20)
    page.wait_for_timeout(300)
    if not _click_visible_option(page, value):
        page.keyboard.press("Enter")


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
    btn = page.locator(
        'form button[type="submit"], form input[type="submit"], '
        'button:has-text("Submit Application"), button:has-text("Submit application"), '
        'button:has-text("Submit")'
    ).first
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
    error is showing, 'unverified' if neither."""
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
    return "unverified"


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
