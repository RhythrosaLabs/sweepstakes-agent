"""
Sweepstakes Agent — Gradio Web Dashboard

Tabs:
  1. Dashboard — Stats, recent entries, quick actions
  2. Discover — Find sweepstakes with live progress
  3. Enter — Enter discovered or custom-URL sweepstakes
  4. History — Full entry log with filtering & value tracking
  5. Profile — Manage entrant PII (saved to .env)
  6. Settings — Agent & browser configuration
"""

import asyncio
import json
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

import gradio as gr

from sweepstakes.config import EntrantProfile, SweepstakesConfig
from sweepstakes.tracker import EntryTracker
from sweepstakes.validators import quick_validate, validate_sweepstakes

# Global state
_config = SweepstakesConfig()
_profile = EntrantProfile()
_tracker = EntryTracker(_config.entry_log_path)
_discovered: list[dict] = []
_progress_log: list[str] = []
_running = False


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    _progress_log.append(f"[{ts}] {msg}")
    if len(_progress_log) > 200:
        _progress_log.pop(0)


def _progress_text():
    return "\n".join(_progress_log[-50:])


# ── Tab 1: Dashboard ─────────────────────────────────────────

def build_dashboard():
    with gr.Column():
        gr.Markdown("# Sweepstakes Agent Dashboard")

        with gr.Row():
            with gr.Column(scale=1):
                stats_md = gr.Markdown(_render_stats(), elem_id="stats")
            with gr.Column(scale=1):
                profile_md = gr.Markdown(_render_profile_card(), elem_id="profile-card")

        with gr.Row():
            recent_md = gr.Markdown(_render_recent(), elem_id="recent")

        refresh_btn = gr.Button("Refresh", size="sm")
        refresh_btn.click(
            fn=lambda: (_render_stats(), _render_profile_card(), _render_recent()),
            outputs=[stats_md, profile_md, recent_md],
        )

    return stats_md, profile_md, recent_md


def _render_stats():
    stats = _tracker.get_stats()
    total = stats.get("total_entries", 0)
    entered = stats.get("entered", 0)
    failed = stats.get("failed", 0)
    skipped = stats.get("skipped", 0)
    rate = stats.get("success_rate", "N/A")
    value = stats.get("total_prize_value", "$0")
    return f"""### Statistics
| Metric | Value |
|--------|-------|
| Total entries | **{total}** |
| Entered | **{entered}** |
| Failed | **{failed}** |
| Skipped | **{skipped}** |
| Success rate | **{rate}** |
| Est. prize pool | **{value}** |
| Discovered (session) | **{len(_discovered)}** |
"""


def _render_profile_card():
    p = _profile
    filled = p.filled_count
    total = p.total_fields
    pct = int(filled / total * 100) if total > 0 else 0
    bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
    return f"""### Profile: {p.first_name or '—'} {p.last_name or '—'}
**Completeness:** {bar} {pct}% ({filled}/{total})

| Field | Value |
|-------|-------|
| Email | {p.email or '—'} |
| Location | {p.city or '—'}, {p.state or '—'} {p.zip_code or '—'} |
| Country | {p.country or '—'} |
"""


def _render_recent():
    recent = _tracker.get_recent(5)
    if not recent:
        return "### Recent Entries\nNo entries yet."
    rows = []
    for e in recent:
        icon = "✅" if e.get("status") == "entered" else "❌" if e.get("status") == "failed" else "⏭️"
        val = e.get("estimated_value") or "—"
        name = e.get("name", "?")[:35]
        date = e.get("entry_date", "?")[:10]
        rows.append(f"| {icon} | {name} | {val} | {date} |")
    table = "\n".join(rows)
    return f"""### Recent Entries
| | Name | Value | Date |
|-|------|-------|------|
{table}
"""


# ── Tab 2: Discover ──────────────────────────────────────────

def build_discover_tab():
    with gr.Column():
        gr.Markdown("## Discover Sweepstakes")

        with gr.Row():
            with gr.Column(scale=2):
                sites_box = gr.Textbox(
                    label="Aggregator Sites (one per line)",
                    value="\n".join(_config.aggregator_sites),
                    lines=6,
                )
            with gr.Column(scale=1):
                max_slider = gr.Slider(1, 20, _config.max_entries_per_run, step=1, label="Max to find")
                cats_box = gr.Textbox(
                    label="Categories (comma-separated, optional)",
                    value=", ".join(_config.categories_of_interest) if _config.categories_of_interest else "",
                    placeholder="cash, travel, electronics",
                )

        discover_btn = gr.Button("Start Discovery", variant="primary", size="lg")
        stop_btn = gr.Button("Stop", variant="stop", size="sm", visible=False)

        progress_box = gr.Textbox(label="Live Progress", lines=12, interactive=False)
        results_md = gr.Markdown("*Press Start to discover sweepstakes*")

    # Event wiring happens in create_app() so we can cross-reference enter_choices
    return discover_btn, stop_btn, progress_box, results_md, sites_box, max_slider, cats_box


def _run_discovery(sites_text, max_entries, categories):
    global _discovered, _running
    _progress_log.clear()
    _running = True

    sites = [s.strip() for s in sites_text.strip().split("\n") if s.strip()]
    _config.aggregator_sites = sites
    _config.max_entries_per_run = int(max_entries)
    _config.categories_of_interest = [c.strip() for c in categories.split(",") if c.strip()] if categories else []

    _log("Starting discovery...")
    _log(f"Sites: {', '.join(sites[:3])}{'…' if len(sites) > 3 else ''}")
    _log(f"Max: {max_entries}")

    def step_callback(browser_state, agent_output, step):
        url = ""
        if browser_state and hasattr(browser_state, "url"):
            url = browser_state.url or ""
        action = ""
        if agent_output and hasattr(agent_output, "action"):
            action_list = agent_output.action if isinstance(agent_output.action, list) else [agent_output.action]
            names = []
            for a in action_list:
                if a and hasattr(a, "model_dump"):
                    d = a.model_dump(exclude_unset=True)
                    for k, v in d.items():
                        if v is not None:
                            names.append(k)
            action = ", ".join(names) if names else ""
        thought = ""
        if agent_output and hasattr(agent_output, "current_state"):
            cs = agent_output.current_state
            if cs and hasattr(cs, "next_goal"):
                thought = cs.next_goal or ""
        parts = [f"Step {step}"]
        if action:
            parts.append(f"▸ {action}")
        if thought:
            parts.append(f"💭 {thought[:80]}")
        if url:
            parts.append(f"🌐 {url[:60]}")
        _log(" | ".join(parts))

    from sweepstakes.agent import register_step_callback, clear_step_callbacks, discover_sweepstakes

    clear_step_callbacks()
    register_step_callback(step_callback)

    try:
        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(discover_sweepstakes(_config, _profile, _tracker, on_step=None))
        loop.close()
    except Exception as e:
        _log(f"Error: {e}")
        results = []

    _running = False
    _discovered = results

    # Validate each
    for sw in _discovered:
        url = sw.get("url", "")
        if url:
            v = quick_validate(
                url=url,
                name=sw.get("name", ""),
                sponsor=sw.get("sponsor", ""),
                trusted_sponsors=_config.trusted_sponsors,
            )
            sw["_validation"] = v

    md = _render_discovered()
    _log(f"Discovery complete: {len(_discovered)} sweepstakes found")
    return md, _progress_text(), gr.update(visible=True), gr.update(visible=False), gr.update(choices=_get_entry_choices(), value=[])


def _render_discovered():
    if not _discovered:
        return "### No sweepstakes found\nTry different sites or broader categories."

    parts = [f"### Found {len(_discovered)} Sweepstakes\n"]
    for i, sw in enumerate(_discovered, 1):
        name = sw.get("name", "Unknown")
        url = sw.get("url", "")
        prize = sw.get("prize", "?")
        value = sw.get("estimated_value", "")
        end = sw.get("end_date", "?")
        sponsor = sw.get("sponsor", "?")
        freq = sw.get("entry_frequency", "one_time")
        conf = sw.get("confidence", "?")

        val = quick_validate(
            url=url,
            name=name,
            sponsor=sponsor,
            trusted_sponsors=_config.trusted_sponsors,
        )
        safety = "🟢" if val.score >= 70 else "🟡" if val.score >= 40 else "🔴"

        parts.append(f"""---
#### {i}. {safety} {name}
- **Prize:** {prize} {f'({value})' if value else ''}
- **Sponsor:** {sponsor}
- **Ends:** {end} | **Frequency:** {freq} | **Confidence:** {conf}
- **URL:** [{url[:60]}{'…' if len(url) > 60 else ''}]({url})
- **Safety score:** {val.score}/100
""")
        if val.red_flags:
            parts.append(f"  - ⚠️ {', '.join(val.red_flags[:3])}")
        if val.positive_signals:
            parts.append(f"  - ✅ {', '.join(val.positive_signals[:3])}")

    return "\n".join(parts)


# ── Tab 3: Enter ──────────────────────────────────────────────

def build_enter_tab():
    with gr.Column():
        gr.Markdown("## Enter Sweepstakes")

        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown("### Discovered Sweepstakes")
                enter_choices = gr.CheckboxGroup(
                    label="Select to enter",
                    choices=_get_entry_choices(),
                )
                refresh_choices = gr.Button("Refresh list", size="sm")
            with gr.Column(scale=1):
                gr.Markdown("### — or enter by URL —")
                url_input = gr.Textbox(label="Direct URL", placeholder="https://...")
                url_enter_btn = gr.Button("Enter URL", size="sm")

        enter_btn = gr.Button("Enter Selected", variant="primary", size="lg")
        entry_progress = gr.Textbox(label="Entry Progress", lines=10, interactive=False)
        entry_result = gr.Markdown("")

    refresh_choices.click(
        fn=lambda: gr.update(choices=_get_entry_choices(), value=[]),
        outputs=[enter_choices],
    )

    enter_btn.click(
        fn=_run_entry_batch,
        inputs=[enter_choices],
        outputs=[entry_result, entry_progress],
    )

    url_enter_btn.click(
        fn=_run_url_entry,
        inputs=[url_input],
        outputs=[entry_result, entry_progress],
    )

    return enter_choices, entry_progress, entry_result


def _get_entry_choices():
    if not _discovered:
        return []
    return [f"{i + 1}. {sw.get('name', '?')[:50]}" for i, sw in enumerate(_discovered)]


def _run_entry_batch(selected):
    if not selected:
        return "Select at least one sweepstakes.", ""

    _progress_log.clear()
    results = []

    def step_cb(browser_state, agent_output, step):
        thought = ""
        if agent_output and hasattr(agent_output, "current_state"):
            cs = agent_output.current_state
            if cs and hasattr(cs, "next_goal"):
                thought = cs.next_goal or ""
        _log(f"Step {step}: {thought[:80]}" if thought else f"Step {step}")

    from sweepstakes.agent import enter_sweepstakes, register_step_callback, clear_step_callbacks

    clear_step_callbacks()
    register_step_callback(step_cb)

    loop = asyncio.new_event_loop()
    for sel in selected:
        idx = int(sel.split(".")[0]) - 1
        if 0 <= idx < len(_discovered):
            sw = _discovered[idx]
            _log(f"\n▸ Entering: {sw.get('name', '?')}")
            try:
                ok = loop.run_until_complete(
                    enter_sweepstakes(sw, _config, _profile, _tracker, on_step=None)
                )
                results.append((sw.get("name", "?"), ok))
            except Exception as e:
                _log(f"Error: {e}")
                results.append((sw.get("name", "?"), False))
    loop.close()

    md = "### Entry Results\n\n"
    for name, ok in results:
        md += f"- {'✅' if ok else '❌'} {name}\n"

    stats = _tracker.get_stats()
    md += f"\n**Success rate:** {stats.get('success_rate', 'N/A')}"
    return md, _progress_text()


def _run_url_entry(url):
    if not url or not url.strip():
        return "Enter a URL.", ""
    _progress_log.clear()
    _log(f"Entering: {url.strip()}")

    from sweepstakes.agent import enter_sweepstakes, register_step_callback, clear_step_callbacks

    clear_step_callbacks()
    sw = {"name": "Direct Entry", "url": url.strip(), "sponsor": "Unknown",
          "prize": "Unknown", "end_date": "Unknown"}

    loop = asyncio.new_event_loop()
    try:
        ok = loop.run_until_complete(
            enter_sweepstakes(sw, _config, _profile, _tracker, on_step=None)
        )
    except Exception as e:
        _log(f"Error: {e}")
        ok = False
    loop.close()

    return f"### {'✅ Success' if ok else '❌ Failed'}\n{url}", _progress_text()


# ── Tab 4: History ────────────────────────────────────────────

def build_history_tab():
    with gr.Column():
        gr.Markdown("## Entry History")

        with gr.Row():
            filter_dd = gr.Dropdown(
                ["All", "Entered", "Failed", "Skipped"],
                value="All", label="Filter",
            )
            refresh_hist = gr.Button("Refresh", size="sm")

        history_md = gr.Markdown(_render_history("All"))

    filter_dd.change(fn=_render_history, inputs=[filter_dd], outputs=[history_md])
    refresh_hist.click(fn=_render_history, inputs=[filter_dd], outputs=[history_md])

    return history_md


def _render_history(filter_status):
    _tracker.reload()
    entries = _tracker.entries

    if filter_status != "All":
        entries = [e for e in entries if e.get("status") == filter_status.lower()]

    if not entries:
        return "No entries match this filter."

    rows = []
    for e in reversed(entries):
        status = e.get("status", "")
        icon = "✅" if status == "entered" else "❌" if status == "failed" else "⏭️"
        val = e.get("estimated_value") or "—"
        name = e.get("name", "?")[:40]
        sponsor = e.get("sponsor", "?")[:20]
        date = e.get("entry_date", "?")[:10]
        source = e.get("source_site", "?")[:15]
        rows.append(
            f"| {icon} | {name} | {sponsor} | {val} | {date} | {source} |"
        )

    table = "\n".join(rows[:50])
    stats = _tracker.get_stats()
    return f"""**Showing {len(rows)} entries** (total: {stats.get('total_entries', 0)}) | Prize pool: {stats.get('total_prize_value', '$0')} | Success: {stats.get('success_rate', 'N/A')}

| | Name | Sponsor | Value | Date | Source |
|-|------|---------|-------|------|--------|
{table}
"""


# ── Tab 5: Profile ────────────────────────────────────────────

def build_profile_tab():
    with gr.Column():
        gr.Markdown("## Entrant Profile")
        gr.Markdown("*Your info is stored locally in `.env` and masked from the AI via `sensitive_data`.*")

        with gr.Row():
            with gr.Column():
                first = gr.Textbox(label="First Name", value=_profile.first_name)
                last = gr.Textbox(label="Last Name", value=_profile.last_name)
                email = gr.Textbox(label="Email", value=_profile.email)
                phone = gr.Textbox(label="Phone", value=_profile.phone)
                dob = gr.Textbox(label="Date of Birth", value=_profile.date_of_birth, placeholder="MM/DD/YYYY")
                age = gr.Textbox(label="Age", value=_profile.age)
            with gr.Column():
                street = gr.Textbox(label="Street Address", value=_profile.street_address)
                city = gr.Textbox(label="City", value=_profile.city)
                state = gr.Textbox(label="State", value=_profile.state)
                zipcode = gr.Textbox(label="ZIP Code", value=_profile.zip_code)
                country = gr.Textbox(label="Country", value=_profile.country)
                instagram = gr.Textbox(label="Instagram Handle", value=_profile.instagram_handle)
                twitter = gr.Textbox(label="Twitter Handle", value=_profile.twitter_handle)
                facebook = gr.Textbox(label="Facebook Name", value=_profile.facebook_name)

        save_btn = gr.Button("Save Profile", variant="primary")
        save_status = gr.Markdown("")

    inputs = [first, last, email, phone, dob, age, street, city, state, zipcode, country, instagram, twitter, facebook]
    save_btn.click(fn=_save_profile, inputs=inputs, outputs=[save_status])

    return save_btn, save_status


def _save_profile(first, last, email, phone, dob, age, street, city, state, zipcode, country, ig, tw, fb):
    global _profile
    _profile.first_name = first
    _profile.last_name = last
    _profile.email = email
    _profile.phone = phone
    _profile.date_of_birth = dob
    _profile.age = age
    _profile.street_address = street
    _profile.city = city
    _profile.state = state
    _profile.zip_code = zipcode
    _profile.country = country
    _profile.instagram_handle = ig
    _profile.twitter_handle = tw
    _profile.facebook_name = fb

    env_path = Path(__file__).resolve().parent / ".env"
    env_map = {
        "SWEEPS_FIRST_NAME": first,
        "SWEEPS_LAST_NAME": last,
        "SWEEPS_EMAIL": email,
        "SWEEPS_PHONE": phone,
        "SWEEPS_DOB": dob,
        "SWEEPS_AGE": age,
        "SWEEPS_ADDRESS": street,
        "SWEEPS_CITY": city,
        "SWEEPS_STATE": state,
        "SWEEPS_ZIP": zipcode,
        "SWEEPS_COUNTRY": country,
        "SWEEPS_INSTAGRAM": ig,
        "SWEEPS_TWITTER": tw,
        "SWEEPS_FACEBOOK": fb,
    }

    # Preserve non-profile keys
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    existing.update(env_map)

    lines = []
    for k, v in existing.items():
        if v:
            lines.append(f"{k}={v}")
    env_path.write_text("\n".join(lines) + "\n")

    pct = int(_profile.filled_count / _profile.total_fields * 100) if _profile.total_fields else 0
    return f"✅ Saved — Profile {pct}% complete ({_profile.filled_count}/{_profile.total_fields} fields)"


# ── Tab 6: Settings ───────────────────────────────────────────

def build_settings_tab():
    with gr.Column():
        gr.Markdown("## Settings")

        with gr.Row():
            with gr.Column():
                model_dd = gr.Dropdown(
                    [
                        "claude-haiku-4-5",          # $1/$5 per MTok — fastest & cheapest
                        "claude-sonnet-4-6",         # $3/$15 per MTok — balanced (default)
                        "claude-opus-4-6",           # $5/$25 per MTok — most capable
                        "claude-3-5-haiku-20241022", # $0.80/$4 per MTok — legacy budget
                        "claude-3-haiku-20240307",   # $0.25/$1.25 per MTok — ultra budget
                    ],
                    value=_config.llm_model,
                    label="LLM Model",
                    info="Cheaper models work fine for simple entries; use Sonnet/Opus for complex forms",
                )
                max_entries = gr.Slider(1, 20, _config.max_entries_per_run, step=1, label="Max entries per run")
                min_score = gr.Slider(0, 100, _config.min_safety_score, step=5, label="Min safety score")
            with gr.Column():
                headless = gr.Checkbox(label="Headless browser", value=_config.headless)
                demo = gr.Checkbox(label="Demo mode (slow animation)", value=_config.demo_mode)
                min_age = gr.Number(label="Min age requirement", value=_config.min_age_requirement)
                country = gr.Textbox(label="Eligible country", value=_config.eligible_country)

        with gr.Row():
            sites = gr.Textbox(
                label="Aggregator sites (one per line)",
                value="\n".join(_config.aggregator_sites),
                lines=5,
            )
            sponsors = gr.Textbox(
                label="Trusted sponsors (one per line)",
                value="\n".join(_config.trusted_sponsors),
                lines=5,
            )

        save_settings = gr.Button("Save Settings", variant="primary")
        settings_status = gr.Markdown("")

    save_settings.click(
        fn=_save_settings,
        inputs=[model_dd, max_entries, min_score, headless, demo, min_age, country, sites, sponsors],
        outputs=[settings_status],
    )
    return save_settings, settings_status


def _save_settings(model, max_e, min_sc, headless, demo, min_age, country, sites, sponsors):
    global _config
    _config.llm_model = model
    _config.max_entries_per_run = int(max_e)
    _config.min_safety_score = int(min_sc)
    _config.headless = headless
    _config.demo_mode = demo
    _config.min_age_requirement = int(min_age)
    _config.eligible_country = country
    _config.aggregator_sites = [s.strip() for s in sites.strip().split("\n") if s.strip()]
    _config.trusted_sponsors = [s.strip().lower() for s in sponsors.strip().split("\n") if s.strip()]

    # Save settings to .env
    env_path = Path(__file__).resolve().parent / ".env"
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    existing["SWEEPS_LLM_MODEL"] = model
    existing["SWEEPS_MAX_ENTRIES"] = str(int(max_e))
    existing["SWEEPS_HEADLESS"] = str(headless).lower()
    existing["SWEEPS_DEMO_MODE"] = str(demo).lower()
    lines = [f"{k}={v}" for k, v in existing.items() if v]
    env_path.write_text("\n".join(lines) + "\n")

    return "✅ Settings saved"


# ── Build App ─────────────────────────────────────────────────

def create_app():
    theme = gr.themes.Soft(
        primary_hue="purple",
        secondary_hue="blue",
        font=gr.themes.GoogleFont("Inter"),
    )
    with gr.Blocks(
        title="Sweepstakes Agent",
        theme=theme,
    ) as app:
        with gr.Tabs():
            with gr.Tab("Dashboard"):
                build_dashboard()
            with gr.Tab("Discover"):
                discover_btn, stop_btn, progress_box, results_md, sites_box, max_slider, cats_box = build_discover_tab()
            with gr.Tab("Enter"):
                enter_choices, entry_progress, entry_result = build_enter_tab()
            with gr.Tab("History"):
                build_history_tab()
            with gr.Tab("Profile"):
                build_profile_tab()
            with gr.Tab("Settings"):
                build_settings_tab()

        # Wire discovery to also update enter_choices across tabs
        discover_btn.click(
            fn=_run_discovery,
            inputs=[sites_box, max_slider, cats_box],
            outputs=[results_md, progress_box, discover_btn, stop_btn, enter_choices],
        )

    return app


def launch_ui(port: int = 7860, share: bool = False):
    app = create_app()
    app.launch(
        server_port=port,
        share=share,
        inbrowser=True,
        app_kwargs={
            "docs_url": None,
        },
    )


if __name__ == "__main__":
    launch_ui()
