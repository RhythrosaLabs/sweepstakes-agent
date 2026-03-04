"""
Pydantic models for structured agent outputs.

Using output_model_schema with browser-use ensures the agent returns
properly typed, validated data instead of raw text we have to regex-parse.
"""

from pydantic import BaseModel, Field


class DiscoveredSweepstakes(BaseModel):
    """A single sweepstakes found during discovery."""

    name: str = Field(description="Name/title of the sweepstakes")
    url: str = Field(description="Direct URL to the entry page")
    sponsor: str = Field(description="Sponsor or brand name")
    prize: str = Field(description="Prize description and approximate value")
    estimated_value: str = Field(
        default="Unknown",
        description="Estimated dollar value of the prize, e.g. '$500' or '$15,000'",
    )
    end_date: str = Field(description="Entry deadline / end date")
    entry_method: str = Field(
        default="online_form",
        description="How to enter: online_form, email, social_media, instant_win",
    )
    entry_frequency: str = Field(
        default="one_time",
        description="How often you can enter: one_time, daily, weekly",
    )
    eligibility: str = Field(
        default="US, 18+",
        description="Who can enter: country and age requirements",
    )
    source_site: str = Field(
        default="",
        description="Which aggregator site this was found on",
    )
    confidence: str = Field(
        default="high",
        description="How confident you are this is legitimate: high, medium, low",
    )


class DiscoveryResult(BaseModel):
    """Structured output from the discovery agent."""

    sweepstakes: list[DiscoveredSweepstakes] = Field(
        default_factory=list,
        description="List of discovered sweepstakes that are free and legitimate",
    )
    sites_visited: int = Field(
        default=0,
        description="Number of aggregator sites successfully visited",
    )
    total_found: int = Field(
        default=0,
        description="Total sweepstakes found before filtering",
    )
    total_filtered: int = Field(
        default=0,
        description="Number filtered out (expired, paid, scam, etc.)",
    )
    summary: str = Field(
        default="",
        description="Brief summary of the discovery session",
    )


class EntryResult(BaseModel):
    """Structured output from the entry agent."""

    success: bool = Field(description="Whether the entry was submitted successfully")
    confirmation_text: str = Field(
        default="",
        description="Any confirmation message shown after submission",
    )
    reference_number: str = Field(
        default="",
        description="Any reference/confirmation number received",
    )
    concerns: list[str] = Field(
        default_factory=list,
        description="Any concerns or warnings about this sweepstakes",
    )
    red_flags_found: list[str] = Field(
        default_factory=list,
        description="Any red flags detected (if any, success should be false)",
    )
    notes: str = Field(
        default="",
        description="Additional notes about the entry process",
    )
