"""
Sweepstakes Discovery & Entry Agent — v4

Strategy: Only enter sweepstakes that require minimal info (email + name).
Skip anything requiring phone, DOB, address, CAPTCHA, or complex widgets.
Uses multiple aggregator sites. Detects failures early and moves on.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_pkg_env = Path(__file__).resolve().parent / ".env"
_repo_env = Path(__file__).resolve().parent.parent / ".env"
if _pkg_env.exists():
    load_dotenv(_pkg_env, override=True)
if _repo_env.exists():
    load_dotenv(_repo_env, override=False)

from browser_use import Agent, BrowserProfile, BrowserSession
from browser_use.llm.anthropic.chat import ChatAnthropic

from sweepstakes.config import EntrantProfile, SweepstakesConfig
from sweepstakes.models import DiscoveryResult, EntryResult
from sweepstakes.tracker import EntryTracker, SweepstakesEntry
from sweepstakes.validators import (
    quick_validate,
    validate_sweepstakes,
    format_validation_for_prompt,
)

logger = logging.getLogger("sweepstakes")


# ── Callback bridge ──────────────────────────────────────────

_step_callbacks: list = []


def register_step_callback(cb):
    _step_callbacks.append(cb)


def clear_step_callbacks():
    _step_callbacks.clear()


async def _on_step(browser_state, agent_output, step):
    for cb in _step_callbacks:
        try:
            if asyncio.iscoroutinefunction(cb):
                await cb(browser_state, agent_output, step)
            else:
                cb(browser_state, agent_output, step)
        except Exception:
            pass


# ── LLM helpers ───────────────────────────────────────────────

def get_llm(model: str = "claude-sonnet-4-6"):
    return ChatAnthropic(model=model)


def get_fallback_llm():
    return ChatAnthropic(model="claude-sonnet-4-6")


def get_extraction_llm():
    """Cheaper model for page extraction during discovery."""
    return ChatAnthropic(model="claude-sonnet-4-6")


# ── Sensitive data ────────────────────────────────────────────

def build_sensitive_data(profile: EntrantProfile) -> dict[str, str]:
    """Map PII to placeholder keys. Only include fields that have values."""
    mapping = {
        "x_first_name": profile.first_name,
        "x_last_name": profile.last_name,
        "x_email": profile.email,
        "x_phone": profile.phone,
        "x_date_of_birth": profile.date_of_birth,
        "x_age": profile.age,
        "x_street_address": profile.street_address,
        "x_city": profile.city,
        "x_state": profile.state,
        "x_zip_code": profile.zip_code,
        "x_country": profile.country,
        "x_instagram": profile.instagram_handle,
        "x_twitter": profile.twitter_handle,
        "x_facebook": profile.facebook_name,
    }
    return {k: v for k, v in mapping.items() if v}


def get_available_fields(profile: EntrantProfile) -> tuple[list[str], list[str]]:
    """Returns (available_fields, missing_fields) as human-readable lists."""
    all_fields = {
        "first_name": ("First Name", profile.first_name),
        "last_name": ("Last Name", profile.last_name),
        "email": ("Email", profile.email),
        "phone": ("Phone", profile.phone),
        "date_of_birth": ("Date of Birth", profile.date_of_birth),
        "age": ("Age", profile.age),
        "street_address": ("Street Address", profile.street_address),
        "city": ("City", profile.city),
        "state": ("State", profile.state),
        "zip_code": ("ZIP Code", profile.zip_code),
        "country": ("Country", profile.country),
    }
    available = []
    missing = []
    for key, (label, val) in all_fields.items():
        if val:
            available.append(label)
        else:
            missing.append(label)
    return available, missing


# ── System persona ────────────────────────────────────────────

SWEEPSTAKES_PERSONA = """
## Sweepstakes Entry Specialist

You are an expert at finding and entering free, legitimate sweepstakes.

### Core Rules:
1. SAFETY FIRST — Never enter financial info, never pay, never download
2. EFFICIENCY — Act fast, don't explore. Use search_page and find_elements (zero cost)
3. HANDLE POPUPS — Close cookie banners, newsletter popups, ad overlays with Escape or clicking X
4. SKIP impossible entries — If CAPTCHA can't be solved, if form requires info you don't have, ABORT and call done(success=false)
5. DON'T INVENT DATA — If a required field needs data you don't have, do NOT make up fake values. Instead, call done(success=false, notes="Required field X not available")

### Form-filling with placeholders:
When filling forms, type these placeholder keys (auto-replaced with real values):
- First name → x_first_name
- Last name → x_last_name
- Email → x_email
- Age → x_age
- City → x_city
- State → x_state
- ZIP code → x_zip_code
- Country → x_country

### IMPORTANT — Fields you may NOT have:
- Phone number (x_phone) — If phone is REQUIRED, call done(success=false, notes="Phone number required but not available")
- Date of birth (x_date_of_birth) — If DOB is REQUIRED, call done(success=false, notes="DOB required but not available")
- Street address (x_street_address) — If full address is REQUIRED, call done(success=false, notes="Street address required but not available")

If a phone/DOB/address field exists but is OPTIONAL (not marked required, no asterisk), you can leave it blank and submit.

### Form Patterns:
- Look for: <form>, <input>, <button type="submit">, .entry-form, #sweepstakes-form
- Age gates: click "I am 18+" or if DOB required → abort
- Required checkboxes: CHECK "agree to rules" / "I am 18+"; UNCHECK marketing opt-ins
- Dropdowns: use dropdown_options to see choices, then select_dropdown
- Submit: "Enter", "Submit Entry", "Enter Now", "Submit", "Sign Up"
- Confirmation: "Thank you", "Entry received", "Confirmation", "entered"

### CAPTCHA Handling:
- If a CAPTCHA appears and doesn't auto-solve within 10 seconds, call done(success=false, notes="CAPTCHA blocked entry")
- Do NOT attempt to manually solve CAPTCHAs repeatedly — abort after first failure
"""


# ── Browser profile ──────────────────────────────────────────

def build_browser_profile(config: SweepstakesConfig) -> BrowserProfile:
    return BrowserProfile(
        headless=config.headless,
        demo_mode=config.demo_mode,
        prohibited_domains=[
            "doubleclick.net", "googlesyndication.com", "adservice.google.com",
            "facebook.com/tr", "analytics.google.com",
        ],
        minimum_wait_page_load_time=0.3,
        wait_for_network_idle_page_load_time=0.5,
        wait_between_actions=0.1,
        enable_default_extensions=True,
        captcha_solver=True,
        window_size={"width": 1440, "height": 900},
    )


# ── Aggregator domain detection ──────────────────────────────

AGGREGATOR_DOMAINS = [
    'sweetiessweeps.com', 'sweepstakesadvantage.com', 'contestgirl.com',
    'online-sweepstakes.com', 'sweepstakesbible.com', 'sweepstakesfanatics.com',
    'ilovegiveaways.com', 'sweepstakesmag.com',
]


def is_aggregator_url(url: str) -> bool:
    return any(d in url.lower() for d in AGGREGATOR_DOMAINS)


# ── Discovery ────────────────────────────────────────────────

def build_discovery_task(
    config: SweepstakesConfig,
    profile: EntrantProfile,
    already_entered_urls: set[str],
    site_url: str,
) -> str:
    """Build discovery task for one aggregator site."""
    _, missing_fields = get_available_fields(profile)

    category_filter = ""
    if config.categories_of_interest:
        category_filter = f"\n- **Focus categories:** {', '.join(config.categories_of_interest)}"

    skip_note = ""
    if already_entered_urls:
        skip_list = list(already_entered_urls)[:10]
        skip_note = f"\n- **Skip these URLs (already entered):** {json.dumps(skip_list)}"

    today = datetime.now().strftime("%B %d, %Y")
    count = min(config.max_entries_per_run, 10)

    # Build missing fields warning
    missing_warning = ""
    if missing_fields:
        missing_warning = f"""
## CRITICAL — Skip sweepstakes that REQUIRE these fields (we don't have them):
{', '.join(missing_fields)}

PREFER sweepstakes that only need: email, name, city, state, zip, age.
Sweepstakes that are email-only or email+name are IDEAL.
Skip any that require phone number, date of birth, or full street address as mandatory fields."""

    return f"""Find up to {count} currently-open, free sweepstakes from {site_url} that are EASY TO ENTER.

Today's date: {today}
{missing_warning}

## STRATEGY:
1. Navigate to {site_url} (you're already there)
2. Look for a list of current sweepstakes. Use extract with extract_links=True to get all sweepstakes links and details.
3. For each sweepstakes listing, find the REAL ENTRY URL — the outbound link to the sponsor's entry page (NOT the {site_url} blog post URL)
4. Click into blog posts if needed to find outbound "Enter Now" / "Enter Sweepstakes" links
5. Prioritize: email-only entries, simple forms, instant win games, known brand sponsors

## CRITICAL — URL Rules:
- The entry URL must be an EXTERNAL link (gleam.io, sponsor.com, woobox.com, etc.)
- NOT a blog post URL on {site_url} (these are articles ABOUT sweepstakes, not entry pages)
- If you can't find an external entry URL, skip that sweepstakes

## Requirements — every sweepstakes MUST be:
- FREE to enter (no purchase, no payment)
- Currently open (end date after {today})
- Open to {config.eligible_country} residents, age {config.min_age_requirement}+
- Has online entry (not mail-only)
{category_filter}
{skip_note}

## REJECT immediately:
- "you've won", "claim prize", "guaranteed winner" language
- Payment or subscription required
- Entries requiring phone, DOB, or full address as mandatory fields
- Complex multi-step contests (bracket challenges, photo uploads, etc.)

## Output each sweepstakes with:
- name, url (DIRECT entry URL), sponsor, prize, estimated_value, end_date
- entry_method: online_form / instant_win / email / social_media
- entry_frequency: one_time / daily / weekly
- eligibility: e.g. "US, 18+"
- source_site: "{site_url}"
- confidence: high / medium / low

Also: sites_visited (1), total_found, total_filtered, summary"""


async def discover_sweepstakes(
    config: SweepstakesConfig,
    profile: EntrantProfile,
    tracker: EntryTracker,
    on_step=None,
) -> list[dict]:
    """Discover sweepstakes from aggregator sites. Uses ALL configured sites."""
    logger.info("=" * 60)
    logger.info("  PHASE 1: Discovering Sweepstakes")
    logger.info("=" * 60)

    already_entered = tracker.get_entered_urls()
    llm = get_llm(config.llm_model)
    fallback = get_fallback_llm()
    extraction_llm = get_extraction_llm()
    browser_profile = build_browser_profile(config)

    all_sweepstakes = []
    seen_urls = set()

    # Use ALL aggregator sites, stop when we have enough
    for site_url in config.aggregator_sites:
        if len(all_sweepstakes) >= config.max_entries_per_run * 2:
            logger.info(f"  Have enough candidates ({len(all_sweepstakes)}), skipping remaining sites")
            break

        logger.info(f"  Scraping: {site_url}")
        task = build_discovery_task(config, profile, already_entered, site_url)

        try:
            agent = Agent(
                task=task,
                llm=llm,
                fallback_llm=fallback,
                page_extraction_llm=extraction_llm,
                browser_profile=browser_profile,
                output_model_schema=DiscoveryResult,
                sensitive_data=build_sensitive_data(profile),
                initial_actions=[{"navigate": {"url": site_url}}],
                max_actions_per_step=8,
                use_vision=True,
                max_failures=3,
                enable_planning=False,
                use_thinking=True,
                extend_system_message=SWEEPSTAKES_PERSONA,
                register_new_step_callback=on_step or _on_step,
                generate_gif=False,
                final_response_after_failure=True,
                message_compaction=True,
                loop_detection_enabled=True,
                include_attributes=["href", "data-url", "target", "rel"],
            )

            result = await agent.run(max_steps=20)

            if result and result.final_result():
                raw = result.final_result()
                items = _parse_discovery_result(raw)
                # Deduplicate
                for item in items:
                    url = item.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_sweepstakes.append(item)
                logger.info(f"    Found {len(items)} from {site_url}")
            else:
                logger.info(f"    No results from {site_url}")

        except Exception as e:
            logger.error(f"    Error scraping {site_url}: {e}")
            continue

    # Validate and filter
    validated = []
    for sw in all_sweepstakes:
        url = sw.get("url", "")
        if not url:
            continue

        # Skip if it's still an aggregator URL (discovery failed to find real link)
        if is_aggregator_url(url):
            logger.info(f"    Skipped aggregator URL: {url[:60]}")
            continue

        # Quick validation
        v = quick_validate(
            url=url,
            name=sw.get("name", ""),
            sponsor=sw.get("sponsor", ""),
            trusted_sponsors=config.trusted_sponsors,
        )

        if v.is_valid:
            sw["_validation_score"] = v.score
            validated.append(sw)
            logger.info(f"    PASS {sw.get('name', '?')[:40]} (score: {v.score})")
        else:
            logger.info(f"    FAIL Filtered: {sw.get('name', '?')[:40]} — {'; '.join(v.red_flags[:2])}")

    # Sort by score (highest first), then limit
    validated.sort(key=lambda x: x.get("_validation_score", 50), reverse=True)
    validated = validated[:config.max_entries_per_run]

    logger.info(f"Discovered {len(validated)} valid sweepstakes (filtered from {len(all_sweepstakes)})")
    return validated


def _parse_discovery_result(raw) -> list[dict]:
    """Parse agent's discovery output into list of sweepstakes dicts."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict):
            items = parsed.get("sweepstakes", [])
            return items if isinstance(items, list) else []
        elif isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        if isinstance(raw, str):
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
    return []


# ── Entry ─────────────────────────────────────────────────────

def build_entry_task(
    sweepstakes_info: dict,
    profile: EntrantProfile,
    config: SweepstakesConfig,
    validation_context: str = "",
) -> str:
    """Build compact, action-oriented entry task."""
    sensitive = build_sensitive_data(profile)
    available_fields, missing_fields = get_available_fields(profile)

    missing_text = ""
    if missing_fields:
        missing_text = f"""
## Fields NOT Available (do NOT make up values):
{', '.join(missing_fields)}
If any of these are REQUIRED by the form (marked with *, required attribute, or form won't submit without them):
→ Call done(success=false, notes="Required field [X] not available") immediately.
If they are OPTIONAL, leave them blank."""

    val_section = ""
    if validation_context:
        val_section = f"\n## Pre-entry Safety Check\n{validation_context}\n"

    url = sweepstakes_info.get('url', '')

    # Aggregator URL handling
    aggregator_step = ""
    if is_aggregator_url(url):
        aggregator_step = """
### Step 0: Navigate to Real Entry Page
This URL is a blog post ABOUT the sweepstakes, not the entry page.
- Use extract with extract_links=True: "find the outbound entry link for this sweepstakes"
- Or find_elements with "a[href]" and look for external domain links
- Click the entry link to navigate to the ACTUAL entry page
- If no outbound link found → done(success=false, notes="No entry link found on aggregator page")
"""

    # Available placeholder keys for form filling
    placeholder_keys = []
    key_map = {
        "x_first_name": "First Name",
        "x_last_name": "Last Name",
        "x_email": "Email",
        "x_age": "Age",
        "x_city": "City",
        "x_state": "State",
        "x_zip_code": "ZIP Code",
        "x_country": "Country",
    }
    for key, label in key_map.items():
        if key in sensitive:
            placeholder_keys.append(f"- {label} → type: {key}")

    placeholders_text = "\n".join(placeholder_keys)

    return f"""Enter this sweepstakes by filling and submitting the entry form.

## Target
- **Name:** {sweepstakes_info.get('name', 'Unknown')}
- **URL:** {url}
- **Sponsor:** {sweepstakes_info.get('sponsor', 'Unknown')}
- **Prize:** {sweepstakes_info.get('prize', 'Unknown')}
{val_section}
## Available Data for Form Filling:
{placeholders_text}
{missing_text}

## Steps (follow IN ORDER):
{aggregator_step}
### Step 1: Clear Obstacles (1 action max)
Close any popup, cookie banner, or overlay — Escape key or click X.

### Step 2: Safety Check (1 action)
Use search_page: "credit card|payment|purchase required|SSN|bank account"
- If matches found in the FORM area (not just in Official Rules/Terms text) → done(success=false, notes="Payment required")
- Matches in "Official Rules", "Terms & Conditions", or footer text are OK to ignore

### Step 3: Find the Form
Use find_elements with selector: "form input, form select, form textarea, form button[type=submit], input[type=email], .entry-form, iframe[src*=gleam], iframe[src*=rafflecopter]"
- If no form found: try clicking "Enter Now" / "Enter Sweepstakes" / "Enter" button first
- If form is in an iframe that you can't interact with → done(success=false, notes="Entry form in inaccessible iframe")
- If it's a complex contest (bracket challenge, photo upload, quiz with many steps) → done(success=false, notes="Complex contest - too many steps")

### Step 4: Check Required Fields
Before filling, scan what the form needs:
- If it requires Phone/DOB/Address and you don't have them → done(success=false, notes="Required field [X] not available")
- If it's email-only or email+name → perfect, proceed

### Step 5: Fill the Form
- Click each input field first, then use input action with the placeholder key
- Email → x_email
- Name: single "Full Name" field → "x_first_name x_last_name"; separate fields → fill each
- State dropdown → dropdown_options then select_dropdown with x_state
- CHECK: "agree to rules", "I am 18+"
- UNCHECK: marketing/newsletter opt-ins
- Leave optional fields blank if you don't have the data

### Step 6: Submit
Click the submit button: "Submit", "Enter", "Enter Now", "Submit Entry"
- If CAPTCHA appears: wait 10 seconds for auto-solve. If not solved → done(success=false, notes="CAPTCHA blocked entry")
- Do NOT retry CAPTCHA more than once

### Step 7: Confirm
Use search_page: "thank you|confirmation|entered|received|success|entry submitted|already entered"
- Confirmation found → done(success=true, confirmation_text="[the message]")
- No clear result → done(success=true, notes="Form submitted, no explicit confirmation")

## ABORT immediately if:
- Payment/subscription required
- CAPTCHA can't be solved (abort after 1 attempt, don't loop)
- Required field not available (phone, DOB, address)
- Account creation with password required
- "You've already won" / "claim your prize" scam language
- Complex multi-step contest
- Cross-origin iframe that can't be accessed

## Output: success, confirmation_text, reference_number, concerns, red_flags_found, notes"""


async def enter_sweepstakes(
    sweepstakes_info: dict,
    config: SweepstakesConfig,
    profile: EntrantProfile,
    tracker: EntryTracker,
    browser_session: BrowserSession | None = None,
    on_step=None,
) -> bool:
    """Enter a single sweepstakes. Returns True on success."""
    name = sweepstakes_info.get("name", "Unknown")
    url = sweepstakes_info.get("url", "")

    logger.info(f"  Entering: {name}")
    logger.info(f"  URL: {url}")

    if tracker.has_entered(url):
        logger.info("  Already entered")
        return False

    # Pre-entry validation
    pre_val = quick_validate(
        url=url,
        name=name,
        sponsor=sweepstakes_info.get("sponsor", ""),
        trusted_sponsors=config.trusted_sponsors,
    )

    if not pre_val.is_valid and pre_val.red_flags:
        logger.info(f"  Pre-validation failed: {'; '.join(pre_val.red_flags[:2])}")
        tracker.skip_entry(name, url, f"Pre-validation failed: {'; '.join(pre_val.red_flags)}")
        return False

    validation_context = format_validation_for_prompt(pre_val)
    task = build_entry_task(sweepstakes_info, profile, config, validation_context)

    llm = get_llm(config.llm_model)
    fallback = get_fallback_llm()
    browser_profile = build_browser_profile(config)

    kwargs = dict(
        task=task,
        llm=llm,
        fallback_llm=fallback,
        browser_profile=browser_profile,
        output_model_schema=EntryResult,
        sensitive_data=build_sensitive_data(profile),
        initial_actions=[{"navigate": {"url": url}}],
        max_actions_per_step=8,
        use_vision=True,
        max_failures=3,
        enable_planning=False,
        use_thinking=True,
        extend_system_message=SWEEPSTAKES_PERSONA,
        register_new_step_callback=on_step or _on_step,
        generate_gif=False,
        final_response_after_failure=True,
        message_compaction=True,
        loop_detection_enabled=True,
        include_attributes=["placeholder", "aria-label", "name", "type", "required", "aria-required"],
    )

    if browser_session:
        kwargs["browser_session"] = browser_session
        del kwargs["browser_profile"]

    agent = Agent(**kwargs)

    # Fewer steps since we abort early on problems
    max_steps = 20 if is_aggregator_url(url) else 15
    result = await agent.run(max_steps=max_steps)

    # Parse result
    success = False
    notes = ""
    if result and result.final_result():
        raw = result.final_result()
        success, notes = _parse_entry_result(raw, result)
    else:
        notes = _extract_failure_notes(result)

    entry = SweepstakesEntry(
        name=name,
        url=url,
        sponsor=sweepstakes_info.get("sponsor", "Unknown"),
        prize_description=sweepstakes_info.get("prize", "Unknown"),
        entry_date=datetime.now().isoformat(),
        end_date=sweepstakes_info.get("end_date", "Unknown"),
        status="entered" if success else "failed",
        source_site=sweepstakes_info.get("source_site", "Unknown"),
        validation_confidence=pre_val.confidence,
        notes=notes,
        entry_method=sweepstakes_info.get("entry_method", "online_form"),
        estimated_value=sweepstakes_info.get("estimated_value", ""),
    )
    tracker.add_entry(entry)

    logger.info(f"  {'Entered' if success else 'Failed'}: {name}")
    if not success and notes:
        logger.info(f"    Reason: {notes[:100]}")
    return success


def _parse_entry_result(raw, result) -> tuple[bool, str]:
    """Parse the agent's entry result into (success, notes)."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict):
            success = parsed.get("success", False)
            parts = []
            if parsed.get("confirmation_text"):
                parts.append(f"Confirmed: {parsed['confirmation_text']}")
            if parsed.get("reference_number"):
                parts.append(f"Ref: {parsed['reference_number']}")
            if parsed.get("concerns"):
                concerns = parsed["concerns"]
                if isinstance(concerns, list):
                    parts.append(f"Concerns: {', '.join(concerns)}")
                elif concerns:
                    parts.append(f"Concerns: {concerns}")
            if parsed.get("red_flags_found"):
                flags = parsed["red_flags_found"]
                if isinstance(flags, list):
                    parts.append(f"Red flags: {', '.join(flags)}")
                elif flags:
                    parts.append(f"Red flags: {flags}")
                success = False
            if parsed.get("notes"):
                parts.append(str(parsed["notes"]))
            notes = " | ".join(parts) or str(raw)
            return success, notes
        else:
            notes = str(raw)
            if result.is_done():
                return result.is_successful() or False, notes
            return False, notes
    except (json.JSONDecodeError, TypeError):
        notes = str(raw)
        if result.is_done():
            return result.is_successful() or False, notes
        return False, notes


def _extract_failure_notes(result) -> str:
    """Extract useful info from a failed agent run."""
    if not result:
        return "Agent returned no result"
    try:
        history = result.history
        if history:
            last_items = history[-3:] if len(history) >= 3 else history
            summaries = []
            for item in last_items:
                if hasattr(item, 'result') and item.result:
                    results = item.result if isinstance(item.result, list) else [item.result]
                    for r in results:
                        if hasattr(r, 'extracted_content') and r.extracted_content:
                            summaries.append(str(r.extracted_content)[:100])
                        elif hasattr(r, 'error') and r.error:
                            summaries.append(f"Error: {r.error[:100]}")
            if summaries:
                return "Agent did not complete. Last actions: " + " | ".join(summaries)
    except Exception:
        pass
    return "Agent hit max steps without completing"


# ── Full pipeline ─────────────────────────────────────────────

async def run_sweepstakes_agent(
    config: SweepstakesConfig | None = None,
    profile: EntrantProfile | None = None,
):
    config = config or SweepstakesConfig()
    profile = profile or EntrantProfile()

    missing = profile.validate()
    if missing:
        logger.error(f"Missing: {missing}")
        return

    logger.info("SWEEPSTAKES AGENT v4")
    tracker = EntryTracker(config.entry_log_path)

    sweeps = await discover_sweepstakes(config, profile, tracker)
    if not sweeps:
        logger.warning("No sweepstakes found.")
        return

    entered = 0
    for i, sw in enumerate(sweeps):
        if entered >= config.max_entries_per_run:
            break
        try:
            if await enter_sweepstakes(sw, config, profile, tracker):
                entered += 1
        except Exception as e:
            logger.error(f"Error: {e}")
            tracker.skip_entry(sw.get("name", "?"), sw.get("url", ""), str(e))

    tracker.print_summary()


async def enter_single_url(url: str, config: SweepstakesConfig, profile: EntrantProfile):
    missing = profile.validate()
    if missing:
        logger.error(f"Missing: {missing}")
        return
    tracker = EntryTracker(config.entry_log_path)
    await enter_sweepstakes(
        {"name": "Direct Entry", "url": url.strip(), "sponsor": "Unknown",
         "prize": "Unknown", "end_date": "Unknown"},
        config, profile, tracker,
    )
    tracker.print_summary()


async def discover_only(config: SweepstakesConfig, profile: EntrantProfile):
    tracker = EntryTracker(config.entry_log_path)
    results = await discover_sweepstakes(config, profile, tracker)
    print("\n" + "=" * 60)
    print("  DISCOVERED SWEEPSTAKES")
    print("=" * 60)
    for i, s in enumerate(results, 1):
        print(f"\n  {i}. {s.get('name', '?')}")
        print(f"     URL:   {s.get('url', 'N/A')}")
        print(f"     Prize: {s.get('prize', 'N/A')} ({s.get('estimated_value', '?')})")
        print(f"     Ends:  {s.get('end_date', '?')}")
        print(f"     Enter: {s.get('entry_frequency', 'one_time')}")
    print()


# ── CLI ───────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Sweepstakes Agent v4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m sweepstakes                            # Full: discover + enter
  python -m sweepstakes --discover-only            # Just find
  python -m sweepstakes --enter-url URL            # Enter one
  python -m sweepstakes --max-entries 5             # Limit entries
  python -m sweepstakes --categories "cash,travel"  # Filter
  python -m sweepstakes ui                          # Web dashboard
        """,
    )
    parser.add_argument("--max-entries", type=int)
    parser.add_argument("--categories", type=str)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-demo", action="store_true")
    parser.add_argument("--model", type=str)
    parser.add_argument("--log-path", type=str)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--enter-url", type=str)

    args = parser.parse_args()
    config = SweepstakesConfig()
    profile = EntrantProfile()

    if args.max_entries:
        config.max_entries_per_run = args.max_entries
    if args.categories:
        config.categories_of_interest = [c.strip() for c in args.categories.split(",")]
    if args.headless:
        config.headless = True
    if args.no_demo:
        config.demo_mode = False
    if args.model:
        config.llm_model = args.model
    if args.log_path:
        config.entry_log_path = args.log_path

    if args.enter_url:
        asyncio.run(enter_single_url(args.enter_url, config, profile))
    elif args.discover_only:
        asyncio.run(discover_only(config, profile))
    else:
        asyncio.run(run_sweepstakes_agent(config, profile))


if __name__ == "__main__":
    main()
