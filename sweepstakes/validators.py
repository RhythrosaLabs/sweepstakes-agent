"""
Scam detection and sweepstakes validation.

Dual-mode validation:
  - quick_validate(): Fast URL + name + sponsor check (no page content needed)
  - validate_sweepstakes(): Full page content analysis

Layers:
  1. Domain reputation (blocklist + suspicious TLD check)
  2. Sponsor reputation (trusted brand matching)
  3. URL structure heuristics
  4. Name/title scam language detection
  5. (Full mode) Payment/fee language detection
  6. (Full mode) Excessive personal info requests
  7. (Full mode) Legitimacy signal checks
"""

import re
import logging
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ─── Threat intelligence ──────────────────────────────────────

KNOWN_SCAM_DOMAINS = {
    "prize-grab.com", "sweepstakes-winner.com", "claim-prize.net",
    "instant-winner.com", "free-prize-now.com", "lottery-international.com",
    "mega-sweepstakes.com", "prizezone.net", "prize-survey.com",
    "winner-notification.com", "lottery-results.net", "free-gift-cards.net",
    "prize-claim.org", "sweeps-winner.com", "award-center.net",
}

SUSPICIOUS_TLDS = {
    ".xyz", ".top", ".click", ".buzz", ".gq", ".tk", ".ml",
    ".ga", ".cf", ".loan", ".win", ".bid", ".racing", ".download",
    ".stream", ".webcam", ".date", ".faith", ".review",
}

# Trusted platforms (aggregators, entry widget hosts)
TRUSTED_PLATFORMS = {
    "sweepstakesadvantage.com", "sweetiessweeps.com", "contestgirl.com",
    "online-sweepstakes.com", "sweepstakesbible.com", "sweepstakesfanatics.com",
    "gleam.io", "rafflecopter.com", "promosimple.com", "viralsweep.com",
    "second-street.com", "woobox.com", "shortstack.com", "eprize.com",
    "prizelogic.com", "helloworld.com", "realtime.media",
}

# Well-known brand domains where sweepstakes are legitimate
TRUSTED_BRAND_DOMAINS = {
    "coca-cola.com", "pepsico.com", "amazon.com", "walmart.com", "target.com",
    "nike.com", "apple.com", "samsung.com", "microsoft.com", "google.com",
    "hgtv.com", "foodnetwork.com", "nbc.com", "cbs.com", "abc.com",
    "disney.com", "sony.com", "lg.com", "hp.com", "dell.com",
    "starbucks.com", "mcdonalds.com", "subway.com", "chipotle.com",
    "generalmills.com", "kelloggs.com", "dole.com", "kraftheinz.com",
    "mondelezinternational.com", "hersheys.com", "nestle.com",
    "pg.com", "unilever.com", "clorox.com", "colgate.com",
}

# ─── Detection patterns ──────────────────────────────────────

PAYMENT_PATTERNS = [
    r"credit\s*card", r"debit\s*card",
    r"payment\s*(required|info|details|method)",
    r"purchase\s*(required|necessary|needed)",
    r"buy\s*(now|to\s*enter|something)",
    r"(shipping|processing|handling)\s*(fee|cost|charge)",
    r"subscribe\s*(to\s*enter|required|first)",
    r"pay\s*\$?\d+", r"entry\s*fee",
    r"membership\s*(required|fee)",
    r"\$\d+.*to\s*enter",
    r"wire\s*transfer", r"money\s*order",
    r"bank\s*account", r"routing\s*number",
    r"social\s*security", r"\bSSN\b",
    r"advance\s*fee", r"activation\s*fee",
    r"(send|pay)\s*(us\s*)?\$",
    r"paypal.*to\s*enter",
]

LEGITIMACY_PATTERNS = [
    r"no\s*purchase\s*necessary",
    r"void\s*where\s*prohibited",
    r"official\s*rules",
    r"open\s*to\s*(legal\s*)?residents",
    r"sweepstakes\s*(begins|ends|period)",
    r"random\s*drawing",
    r"odds\s*of\s*winning",
    r"sponsor\s*:",
    r"alternate\s*(method|means)\s*of\s*entry",
    r"\bAMOE\b",
    r"approximate\s*retail\s*value",
    r"\bARV\b",
    r"no\s*cost\s*to\s*enter",
]

EXCESSIVE_INFO_PATTERNS = [
    r"social\s*security\s*(number)?", r"\bSSN\b",
    r"bank\s*(account|routing)", r"credit\s*score",
    r"tax\s*(return|id|identification)", r"passport\s*number",
    r"driver'?s?\s*license\s*number", r"mother'?s?\s*maiden",
    r"place\s*of\s*birth", r"immigration\s*status",
    r"annual\s*(income|salary)", r"employer\s*identification",
]

URGENCY_SCAM_PATTERNS = [
    r"you('ve|'re|\s+have)\s*(already\s*)?won",
    r"claim\s*(your|the)\s*prize\s*now",
    r"act\s*(now|immediately|fast|quickly)",
    r"last\s*chance", r"expires?\s*in\s*\d+\s*(min|hour|second)",
    r"limited\s*time\s*only", r"don'?t\s*miss\s*out",
    r"guaranteed\s*winner", r"you\s*(are|were)\s*selected",
    r"congratulations.*winner", r"100%\s*free\s*prize",
]

# Patterns that appear in scammy *titles/names* (different from page content)
NAME_SCAM_PATTERNS = [
    r"(you|u)\s*(have\s*)?won",
    r"claim\s*(now|your|free|prize)",
    r"free\s*(iphone|ipad|macbook|gift\s*card|money|cash)",
    r"guaranteed\s*(win|prize|money)",
    r"\$\d{4,}.*free",
    r"(click|act)\s*(here|now|fast)",
    r"congratulations",
    r"selected\s*(winner|you)",
    r"limited\s*offer",
    r"earn\s*\$\d+.*per\s*(day|hour)",
]


# ─── Validation result ───────────────────────────────────────

@dataclass
class ValidationResult:
    """Result of sweepstakes validation."""

    is_valid: bool
    confidence: float       # 0.0 - 1.0
    reasons: list[str]      # positive signals
    warnings: list[str]     # concerns but not blockers
    red_flags: list[str]    # dealbreakers

    @property
    def score(self) -> int:
        """0-100 integer score for UI display."""
        return max(0, min(100, int(self.confidence * 100)))

    @property
    def positive_signals(self) -> list[str]:
        """Alias for reasons — used by UI rendering."""
        return self.reasons

    @property
    def summary(self) -> str:
        icon = "✅" if self.is_valid else "❌"
        parts = [f"{icon} {'VALID' if self.is_valid else 'INVALID'} ({self.confidence:.0%})"]
        if self.red_flags:
            parts.append("RED FLAGS: " + "; ".join(self.red_flags))
        if self.warnings:
            parts.append("WARNINGS: " + "; ".join(self.warnings))
        if self.reasons:
            parts.append("POSITIVE: " + "; ".join(self.reasons[:3]))
        return " | ".join(parts)


def _find_matches(patterns: list[str], text: str) -> list[str]:
    """Run a list of regex patterns against text, return matched strings."""
    found = []
    text_lower = text.lower()
    for pattern in patterns:
        m = re.search(pattern, text_lower)
        if m:
            found.append(m.group())
    return found


def check_domain(url: str) -> tuple[list[str], list[str]]:
    """Check URL domain. Returns (red_flags, positive_signals)."""
    red_flags, positives = [], []
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]

        if not domain:
            red_flags.append("Empty or invalid URL")
            return red_flags, positives

        if domain in KNOWN_SCAM_DOMAINS:
            red_flags.append(f"Known scam domain: {domain}")

        for tld in SUSPICIOUS_TLDS:
            if domain.endswith(tld):
                red_flags.append(f"Suspicious TLD: {tld}")

        if domain in TRUSTED_PLATFORMS:
            positives.append(f"Trusted platform: {domain}")

        # Check against brand domains
        for brand in TRUSTED_BRAND_DOMAINS:
            if domain == brand or domain.endswith("." + brand):
                positives.append(f"Known brand domain: {brand}")
                break

        # IP-based URLs
        try:
            socket.inet_aton(domain.split(":")[0])
            red_flags.append("IP-based URL (no domain name)")
        except socket.error:
            pass

        # Very long domains
        if len(domain) > 50:
            red_flags.append(f"Unusually long domain ({len(domain)} chars)")

        # Suspicious URL path patterns
        path = parsed.path.lower()
        if any(x in path for x in ["/wp-admin", "/phishing", "/claim-prize", "/winner"]):
            red_flags.append(f"Suspicious URL path: {path[:40]}")

    except Exception:
        pass
    return red_flags, positives


# ─── Quick validation (URL + name + sponsor only) ────────────

def quick_validate(
    url: str,
    name: str = "",
    sponsor: str = "",
    trusted_sponsors: list[str] | None = None,
) -> ValidationResult:
    """
    Fast pre-check using only URL, sweepstakes name, and sponsor.
    No page content needed — suitable for validating discovery results
    before attempting entry.

    Returns a ValidationResult with score/100 and flags.
    """
    red_flags = []
    warnings = []
    reasons = []
    score = 0.5  # start neutral

    # 1. Domain check
    domain_flags, domain_positives = check_domain(url)
    red_flags.extend(domain_flags)
    reasons.extend(domain_positives)
    score -= 0.2 * len(domain_flags)
    score += 0.1 * len(domain_positives)

    # 2. Name scam patterns
    if name:
        name_hits = _find_matches(NAME_SCAM_PATTERNS, name)
        if name_hits:
            red_flags.extend([f"Scam language in title: '{h}'" for h in name_hits[:2]])
            score -= 0.25 * len(name_hits)

        # Name urgency
        urgency_in_name = _find_matches(URGENCY_SCAM_PATTERNS, name)
        if urgency_in_name:
            red_flags.extend([f"Urgency scam in title: '{h}'" for h in urgency_in_name[:2]])
            score -= 0.2

    # 3. Sponsor check
    if sponsor and trusted_sponsors:
        sponsor_lower = sponsor.lower()
        for ts in trusted_sponsors:
            if ts.lower() in sponsor_lower or sponsor_lower in ts.lower():
                reasons.append(f"Trusted sponsor: {sponsor}")
                score += 0.15
                break
    elif sponsor:
        # Even without a trusted list, a named sponsor is better than none
        if len(sponsor) > 2 and sponsor.lower() not in ("unknown", "n/a", "?", "none"):
            reasons.append(f"Named sponsor: {sponsor}")
            score += 0.05

    # 4. URL has HTTPS
    if url.startswith("https://"):
        reasons.append("HTTPS")
        score += 0.03
    elif url.startswith("http://"):
        warnings.append("No HTTPS — less secure")
        score -= 0.05

    # 5. Check if URL points to known entry widget (gleam, rafflecopter, etc.)
    url_lower = url.lower()
    for platform in ["gleam.io", "rafflecopter.com", "promosimple.com", "viralsweep.com",
                     "woobox.com", "shortstack.com", "second-street.com"]:
        if platform in url_lower:
            reasons.append(f"Uses entry platform: {platform}")
            score += 0.1
            break

    # Clamp
    score = max(0.0, min(1.0, score))

    # Verdict — stricter than full validation since we have less info
    is_valid = score >= 0.4 and len(red_flags) == 0

    return ValidationResult(
        is_valid=is_valid,
        confidence=score,
        reasons=reasons,
        warnings=warnings,
        red_flags=red_flags,
    )


# ─── Full validation (with page content) ─────────────────────

def validate_sweepstakes(
    url: str,
    page_text: str = "",
    trusted_sponsors: list[str] | None = None,
    name: str = "",
    sponsor: str = "",
) -> ValidationResult:
    """
    Comprehensive validation of a sweepstakes using page content.
    Falls back to quick_validate if page_text is empty/short.
    """
    # If no real page content, fall back to quick mode
    if not page_text or len(page_text) < 50:
        return quick_validate(url, name=name or page_text, sponsor=sponsor or "", trusted_sponsors=trusted_sponsors)

    red_flags = []
    warnings = []
    reasons = []
    score = 0.5

    # 1. Domain check
    domain_flags, domain_positives = check_domain(url)
    red_flags.extend(domain_flags)
    reasons.extend(domain_positives)
    score -= 0.15 * len(domain_flags)
    score += 0.1 * len(domain_positives)

    # 2. Payment/fee detection
    payment_hits = _find_matches(PAYMENT_PATTERNS, page_text)
    if payment_hits:
        red_flags.extend([f"Payment language: '{h}'" for h in payment_hits[:3]])
        score -= 0.2 * len(payment_hits)

    # 3. Excessive info detection
    excessive_hits = _find_matches(EXCESSIVE_INFO_PATTERNS, page_text)
    if excessive_hits:
        red_flags.extend([f"Excessive info request: '{h}'" for h in excessive_hits[:3]])
        score -= 0.25 * len(excessive_hits)

    # 4. Urgency/scam language
    urgency_hits = _find_matches(URGENCY_SCAM_PATTERNS, page_text)
    if urgency_hits:
        for h in urgency_hits[:2]:
            warnings.append(f"Urgency language: '{h}'")
        score -= 0.1 * len(urgency_hits)

    # 5. Legitimacy signals (strong positive indicators)
    legit_hits = _find_matches(LEGITIMACY_PATTERNS, page_text)
    if legit_hits:
        reasons.extend([f"Legitimacy signal: '{h}'" for h in legit_hits[:4]])
        score += 0.08 * len(legit_hits)

    # 6. "No Purchase Necessary" — the gold standard
    if re.search(r"no\s*purchase\s*necessary", page_text, re.I):
        reasons.append("'No Purchase Necessary' statement found")
        score += 0.15

    # 7. Official rules
    if re.search(r"official\s*rules", page_text, re.I):
        reasons.append("Official Rules link/text found")
        score += 0.1

    # 8. Trusted sponsor match
    if trusted_sponsors:
        text_lower = page_text.lower()
        for ts in trusted_sponsors:
            if ts.lower() in text_lower:
                reasons.append(f"Trusted sponsor: {ts}")
                score += 0.12
                break

    # 9. Sponsor from metadata
    if sponsor and trusted_sponsors:
        for ts in trusted_sponsors:
            if ts.lower() in sponsor.lower() or sponsor.lower() in ts.lower():
                reasons.append(f"Known sponsor: {sponsor}")
                score += 0.08
                break

    # Clamp
    score = max(0.0, min(1.0, score))

    # Verdict
    is_valid = score >= 0.4 and len(red_flags) == 0
    if red_flags:
        is_valid = False
    elif score < 0.35:
        warnings.append("Low confidence — proceed with caution")

    return ValidationResult(
        is_valid=is_valid,
        confidence=score,
        reasons=reasons,
        warnings=warnings,
        red_flags=red_flags,
    )


def format_validation_for_prompt(result: ValidationResult) -> str:
    """Format validation result for LLM consumption in entry task."""
    lines = [
        f"Pre-entry validation: {'PASS' if result.is_valid else 'FAIL'} "
        f"(confidence: {result.confidence:.0%})"
    ]
    if result.red_flags:
        lines.append("RED FLAGS — DO NOT ENTER:")
        lines.extend(f"  ⛔ {f}" for f in result.red_flags)
    if result.warnings:
        lines.append("WARNINGS:")
        lines.extend(f"  ⚠️  {w}" for w in result.warnings)
    if result.reasons:
        lines.append("POSITIVE SIGNALS:")
        lines.extend(f"  ✅ {r}" for r in result.reasons)
    return "\n".join(lines)
