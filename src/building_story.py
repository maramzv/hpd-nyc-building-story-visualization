"""Turns a building's raw HPD violation records into a six-dimension profile
and an evidence-based narrative sentence.

This is the reusable version of the analysis done ad hoc in
scripts/phase3_derive_taxonomy.py, and it's the core of the "Building Story
Engine" from local project notes. Deterministic, rule-based — no ML.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

NON_COMPLIANCE_STATUSES = {"NOT COMPLIED WITH", "FALSE CERTIFICATION", "INVALID CERTIFICATION"}
ACCEPTED_CERT_STATUSES = {"NOV CERTIFIED ON TIME", "NOV CERTIFIED LATE"}
P90_OVERDUE_DAYS = 9.7 * 365
P99_OVERDUE_DAYS = 25.2 * 365
MIN_CERT_ATTEMPTS_FOR_ENGAGEMENT = 3
# "Recent" violation activity is measured over this window. Started at 365;
# widened to 730 because HPD correction deadlines plus the certification
# back-and-forth routinely run 6-18 months, so a 14-month-old violation is
# often still an active matter, not a cold case - and at 365 days three of
# every four buildings citywide landed in the least-active bucket.
RECENT_WINDOW_DAYS = 730
# A "frozen" violation has had zero recorded activity for at least this many
# years. HPD's correction/certification/re-inspection cycle runs in
# weeks-to-months, so two years of total silence is abandonment, not backlog.
# See _frozen_silent_years() below.
FROZEN_MIN_YEARS = 2
# Certification statuses that mean the violation is resolved (owner certified,
# city accepted) - never "frozen" however old.
FROZEN_RESOLVED_STATUSES = {
    "NOV CERTIFIED ON TIME", "NOV CERTIFIED LATE",
    "LEAD DOCS SUBMITTED, ACCEPTABLE", "COMPLIED IN ACCESS AREA",
}
# Rejected-certification statuses. The owner engaged (falsely, but engaged);
# this feeds the "Resistant" engagement read, so it is its own story, not
# "frozen".
FROZEN_REJECTED_STATUSES = {
    "FALSE CERTIFICATION", "INVALID CERTIFICATION", "LEAD DOCS SUBMITTED, NOT ACCEPTABLE",
}
# p75 of real_defect_count within the Isolated/Widespread candidate pool
# (buildings with >=1 real defect and no Persistent/Chronic recurring
# signature; n=106,669, calibrated via scripts/calibrate_real_defect_count.py
# against the full citywide dataset). Below this, a building's real-defect
# count is still typical for that pool; at/above it, a building sits in the
# top quarter by volume even though nothing has ever recurred - "Isolated"
# stopped being an honest word for a building with, say, 35 one-off defects.
REAL_DEFECT_WIDESPREAD_THRESHOLD = 9

# OrderNumbers that represent recurring ADMINISTRATIVE/FILING obligations rather
# than physical defects. These recur by design (e.g. annually, for every subject
# building) and would falsely inflate Persistent/Chronic pattern detection if not
# excluded. Found via Phase 3 testing: 8 of 25 "Persistent" matches in an 80-building
# sample turned out to be the same annual-filing code (1507). Audited the other top-60
# codes by citywide frequency (data/ordernumber_counts.json) for the same pattern —
# high confidence on the first 5, moderate on the last 2 (posting/certification-type
# obligations that plausibly recur on a compliance calendar, but not as certain as the
# bedbug report). Not exhaustive — only the top 60 of 396 codes were reviewed.
ADMINISTRATIVE_ORDERNUMBERS = {
    "780",   # "OWNER FAILED TO FILE A VALID REGISTRATION STATEMENT" - recurs if unregistered
    "1507",  # "FILE ANNUAL BEDBUG REPORT" - recurs yearly by design
    "700",   # "POST A PROPER NOTICE OF SMOKE DETECTOR REQUIREMENTS" - signage, not the device
    "1501",  # "POST A PROPER NOTICE OF CARBON MONOXIDE DETECTING DEVICE REQUIREMENTS" - signage
    "778",   # "POST AND MAINTAIN A PROPER SIGN...SHOWING THE REGISTRATION NUMBER" - signage
    "484",   # "PROVIDE A COMPLETED CERTIFICATE OF INSPECTION VISITS" - MDL S329 posting (moderate confidence)
    "623",   # "CERTIFY COMPLIANCE WITH LEAD-BASED PAINT HAZARD CONTROL REQUIREMENTS" - Local Law 1 annual cert (moderate confidence)
}

# Generic legal/administrative boilerplate that shows up in almost every
# NOVDescription regardless of what's actually broken (code citations, "adm
# code", "properly repair", etc.) - stripped out before comparing descriptions
# within a recurring signature, so the comparison is measuring the defect
# itself, not the shared legal phrasing every notice is wrapped in.
_DESCRIPTION_STOPWORDS = {
    "PROPERLY", "REPAIR", "REPLACE", "REMOVE", "MAINTAIN", "PROVIDE", "CLEAN",
    "CONDITION", "ADM", "CODE", "HMC", "SECTION", "MDL", "LAW", "REQUIRED",
    "SIMILAR", "MATERIAL", "ACCORDANCE", "DESCRIBED", "NOTICE", "VIOLATION",
    "BUILDING", "APARTMENT", "LOCATED", "ENTIRE", "WHICH", "THEREFORE",
    "SUBJECT", "ABATE", "DEFECTIVE", "BROKEN", "STORY", "FRONT", "REAR",
}


def _defect_keywords(desc):
    """Strip legal boilerplate from a NOVDescription, leaving the words that
    actually identify what's wrong (e.g. MICE, ROACH, PLASTER, LINTEL)."""
    if not desc:
        return set()
    words = re.findall(r"[A-Z]{4,}", desc.upper())
    return {w for w in words if w not in _DESCRIPTION_STOPWORDS}


def _signature_is_coherent(descriptions):
    """A recurring signature (same OrderNumber) can mean the same specific
    defect recurring, or a generic repair code covering many unrelated
    defects (Finding 10: OrderNumber 502 covering 20 different structural
    problems at one building, sharing only the legal code, not the defect).
    Checks whether the descriptions actually share a real defect keyword -
    if the single most common keyword appears in at least 60% of the
    distinct descriptions, this reads as one real recurring problem."""
    distinct = list({d for d in descriptions if d})
    if len(distinct) <= 1:
        return True
    counts = defaultdict(int)
    for d in distinct:
        for kw in _defect_keywords(d):
            counts[kw] += 1
    if not counts:
        return False
    return max(counts.values()) / len(distinct) >= 0.6


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace(".000", ""))
    except Exception:
        return None


def _frozen_silent_years(v, today):
    """If a violation is "frozen", the number of years its record has been
    silent; otherwise None. Shared by the story's frozen count and the map
    timeline's grey state so the two always report the same number.

    Frozen = all of: (1) not resolved, (2) not an administrative filing
    obligation or a rejected certification (each its own story), (3) a real
    correction deadline that has passed, (4) nothing recorded for
    FROZEN_MIN_YEARS+. Deliberately NOT gated on "the status never moved off
    issuance" - one dead-end stamp years ago leaves a violation just as frozen
    as one that was never touched.
    """
    status = v.get("currentstatus") or ""
    if status in FROZEN_RESOLVED_STATUSES:
        return None
    if v.get("ordernumber") in ADMINISTRATIVE_ORDERNUMBERS:
        return None
    if status in FROZEN_REJECTED_STATUSES:
        return None
    deadline = _parse_date(v.get("newcorrectbydate")) or _parse_date(v.get("originalcorrectbydate"))
    if not deadline or deadline >= today:
        return None
    # Last recorded activity. currentstatusdate covers ~100% of rows but a few
    # dozen carry junk (year 9999) - reject future / pre-1970 and fall back to
    # the NOV date.
    last = None
    for s in (v.get("currentstatusdate"), v.get("novissueddate")):
        d = _parse_date(s)
        if d and d <= today and d.year >= 1970:
            last = d
            break
    if not last:
        return None
    years = (today - last).days / 365
    return years if years >= FROZEN_MIN_YEARS else None


def _frozen_state(v, today):
    return _frozen_silent_years(v, today) is not None


@dataclass
class BuildingProfile:
    buildingid: str
    address: str
    active_count: int
    real_defect_count: int
    recent_count: int
    recency_ratio: float
    class_c_total: int
    class_c_recent: int
    class_c_open: int
    class_c_rate: float
    non_compliance_total: int
    non_compliance_recent: int
    accepted_cert: int
    rejected_cert: int
    cert_acceptance_rate: float | None
    max_days_overdue: int
    max_years_overdue: float
    top_sig_notices: int
    top_sig_span_years: float
    top_sig_breadth: int          # distinct apartments the winning signature touched (0 = common-area only)
    top_sig_coherent: bool        # False = generic code covering several different defects, not one recurring problem
    top_sig_ordernumber: str | None  # OrderNumber of the winning signature - lets the UI pull its actual records instead of re-guessing which ones they are
    n_persistent_sigs: int
    n_chronic_sigs: int
    n_defect_visits: int          # distinct dates a real (non-admin) defect was cited, any ordernumber
    defect_visit_span_years: float

    # Process-staleness signals. "Frozen" = correction deadline passed, not
    # resolved, and nothing recorded for FROZEN_MIN_YEARS+ - see
    # _frozen_silent_years(), which the map timeline shares so the counts
    # agree. stale_status_count is a separate softer 5-year signal.
    frozen_overdue_count: int = 0
    frozen_overdue_share: float = 0.0   # / real_defect_count
    frozen_years_max: float = 0.0       # years the longest-silent frozen violation has gone without a record
    stale_status_count: int = 0         # real, uncertified violations with no status change in 5+ years
    timeline_fields_present: bool = False  # False when the cache predates the timeline-field re-pull

    # Assigned dimension levels
    scale: str = ""
    recency: str = ""
    severity: str = ""
    engagement: str = ""
    pattern: str = ""
    backlog_age: str = ""
    long_unresolved: bool = False


def _level_scale(n):
    if n <= 3:
        return "Minimal"
    if n <= 7:
        return "Low"
    if n <= 18:
        return "Moderate"
    if n <= 66:
        return "Large"
    return "Severe"


def _level_recency(ratio):
    # ratio = share of open violations issued within RECENT_WINDOW_DAYS.
    if ratio < 0.15:
        return "Gone quiet"
    if ratio <= 0.70:
        return "Ongoing"
    return "Active surge"


def _level_severity(rate, class_c_open=0):
    # Class C is HPD's immediately-hazardous tier (no heat, no gas, lead,
    # collapse risk). A building with an uncertified one on the books is not
    # "Low" severity however small its share of the total - floor at Elevated.
    if rate < 0.10:
        return "Elevated" if class_c_open >= 1 else "Low"
    if rate <= 0.30:
        return "Elevated"
    if rate <= 0.70:
        return "Severe"
    return "Extreme"


def _level_engagement(accepted, rejected, days_overdue=0):
    attempts = accepted + rejected
    if attempts < MIN_CERT_ATTEMPTS_FOR_ENGAGEMENT:
        # Zero attempts with a deadline already blown 90+ days is not "missing
        # data" - it's the owner never responding. Distinguish that from a
        # building whose violations are simply too new to have been certified
        # yet (nothing overdue, or 1-2 attempts = some engagement, too little
        # to rate the pattern).
        if attempts == 0 and days_overdue >= 90:
            return "Unaddressed"
        return "Too early to tell"
    rate = accepted / attempts
    if rate < 0.30:
        return "Resistant"
    if rate <= 0.70:
        return "Mixed engagement"
    return "Responsive"


def _level_pattern(n_persistent, n_chronic, real_defect_count):
    if n_chronic > 0:
        return "Chronic"
    if n_persistent > 0:
        return "Persistent"
    if real_defect_count == 0:
        # Finding 1: ~1 in 5 buildings citywide have violations on file that
        # are entirely administrative (bedbug filings, registration lapses)
        # with no physical defect ever cited. Distinct from an ordinary
        # Isolated building with one small real problem - same bucket today,
        # different reality.
        return "No real defects"
    if real_defect_count >= REAL_DEFECT_WIDESPREAD_THRESHOLD:
        # A building can rack up dozens of never-repeating real defects and
        # still pass every recurrence check - "Isolated" (a word that reads
        # as "one thing happened") shouldn't cover that. Split purely on
        # volume; severity is already its own dimension, not folded in here.
        return "Widespread"
    return "Isolated"


def _level_backlog(days):
    # `days` = age of the oldest still-open, uncertified missed deadline; 0
    # when nothing is actually overdue. Cutoffs unchanged from the calibrated
    # ramp - only the labels, which used to read too gently ("Aging" for a
    # deadline blown six years ago).
    if days == 0:
        return "Nothing overdue"
    years = days / 365
    if years < 2:
        return "Recently overdue"
    if years <= 9.7:
        return "Years overdue"
    if years <= 25:
        return "Long overdue"
    return "Decades overdue"


def build_profile(buildingid: str, violations: list[dict], today: datetime) -> BuildingProfile:
    """Compute the six-dimension profile for one building from its raw violation rows.

    Every row HPD returned is counted - one ViolationID, one violation. No
    de-duplication: look-alike rows (same apartment/description/date) are often
    real separate citations, e.g. the same condition inspected in different
    years and later carried onto one notice. The story, evidence tab and map
    timeline all count the raw rows, so their totals always match.
    """
    addr = f"{violations[0].get('housenumber','')} {violations[0].get('streetname','')}, {violations[0].get('boro','')}"
    active_count = len(violations)
    real_defect_count = sum(1 for v in violations if v.get("ordernumber") not in ADMINISTRATIVE_ORDERNUMBERS)

    recent_count = class_c_recent = class_c_total = class_c_open = 0
    non_compliance_total = non_compliance_recent = 0
    accepted_cert = rejected_cert = 0
    max_days_overdue = 0
    # Process-staleness (Findings 8/9) - only meaningful once the violation
    # cache carries the timeline fields (added 2026-09-01). On an older cache
    # these stay at their zero defaults and timeline_fields_present is False.
    timeline_present = any("currentstatusdate" in v for v in violations)
    frozen_overdue_count = stale_status_count = 0
    frozen_years_max = 0.0
    # Building-wide signatures (same OrderNumber, ANY apartment) - not
    # apartment-scoped. Finding 2: apartment-only grouping structurally can't
    # see a defect recurring across different units at the same building
    # (e.g. a mice infestation cited in 9 different apartments over 7 years),
    # and building-wide recurrence is a strict superset of apartment-only
    # (verified: no building ever downgrades under this change), so it
    # replaces apartment-scoped grouping entirely rather than sitting beside it.
    signatures = defaultdict(dict)          # ordernumber -> {novid: nov_date}
    sig_apartments = defaultdict(set)       # ordernumber -> {apartments touched}
    sig_descriptions = defaultdict(dict)    # ordernumber -> {novid: description}
    # Distinct calendar dates a REAL defect was cited, regardless of whether
    # it was the same ordernumber recurring - captures a building that keeps
    # getting hit with a *different* problem at nearly every inspection
    # (Finding: 588 Gates Ave / 713 Tilden St), which the signature-based
    # pattern logic above can't see since it only tracks same-code recurrence.
    defect_visit_dates = set()

    for v in violations:
        nov_date = _parse_date(v.get("novissueddate"))
        cls = v.get("class")
        status = v.get("currentstatus")
        is_recent = bool(nov_date and (today - nov_date).days <= RECENT_WINDOW_DAYS)

        if is_recent:
            recent_count += 1
            if cls == "C":
                class_c_recent += 1
            if status in NON_COMPLIANCE_STATUSES:
                non_compliance_recent += 1
        if cls == "C":
            class_c_total += 1
        if status in NON_COMPLIANCE_STATUSES:
            non_compliance_total += 1
        if status in ACCEPTED_CERT_STATUSES:
            accepted_cert += 1
        if status in ("FALSE CERTIFICATION", "INVALID CERTIFICATION"):
            rejected_cert += 1

        deadline = _parse_date(v.get("newcorrectbydate")) or _parse_date(v.get("originalcorrectbydate"))
        # Finding 7: a violation the owner already certified fixed (even
        # years late) isn't a currently-outstanding deadline - counting it
        # toward backlog age is how a 2008 record reads as "18 years overdue"
        # today. Only uncertified violations can push this number.
        certified = status in ACCEPTED_CERT_STATUSES
        if deadline and deadline < today and not certified:
            max_days_overdue = max(max_days_overdue, (today - deadline).days)
        if cls == "C" and not certified:
            class_c_open += 1

        # Process-staleness. stale_status_count is a softer 5-year signal over
        # any real uncertified violation; frozen is the sharp one - see
        # _frozen_silent_years().
        if timeline_present:
            is_real = v.get("ordernumber") not in ADMINISTRATIVE_ORDERNUMBERS
            status_date = _parse_date(v.get("currentstatusdate"))
            if is_real and not certified and status_date and (today - status_date).days / 365 >= 5:
                stale_status_count += 1
            silent_years = _frozen_silent_years(v, today)
            if silent_years is not None:
                frozen_overdue_count += 1
                frozen_years_max = max(frozen_years_max, silent_years)

        ordernumber = v.get("ordernumber")
        novid = v.get("novid")
        if nov_date and ordernumber not in ADMINISTRATIVE_ORDERNUMBERS:
            defect_visit_dates.add(nov_date.date())
        if nov_date and novid and ordernumber not in ADMINISTRATIVE_ORDERNUMBERS:
            if novid not in signatures[ordernumber] or nov_date < signatures[ordernumber][novid]:
                signatures[ordernumber][novid] = nov_date
                sig_descriptions[ordernumber][novid] = v.get("novdescription")
            apt = v.get("apartment")
            if apt:
                sig_apartments[ordernumber].add(apt)

    recurring_sigs = []
    for ordernumber, novid_dates in signatures.items():
        dates = list(novid_dates.values())
        if len(dates) >= 2:
            span_years = (max(dates) - min(dates)).days / 365
            breadth = len(sig_apartments[ordernumber])
            coherent = _signature_is_coherent(list(sig_descriptions[ordernumber].values()))
            recurring_sigs.append((len(dates), span_years, breadth, coherent, ordernumber))
    recurring_sigs.sort(key=lambda x: -x[0])

    top_sig_notices, top_sig_span, top_sig_breadth, top_sig_coherent, top_sig_ordernumber = (
        recurring_sigs[0] if recurring_sigs else (0, 0.0, 0, True, None)
    )
    n_persistent = sum(1 for n, s, *_ in recurring_sigs if n >= 3 and s >= 2)
    n_chronic = sum(1 for n, s, *_ in recurring_sigs if n >= 10 and s >= 5)
    cert_attempts = accepted_cert + rejected_cert

    n_defect_visits = len(defect_visit_dates)
    defect_visit_span_years = (
        (max(defect_visit_dates) - min(defect_visit_dates)).days / 365
        if n_defect_visits >= 2 else 0.0
    )

    p = BuildingProfile(
        buildingid=buildingid,
        address=addr,
        active_count=active_count,
        real_defect_count=real_defect_count,
        recent_count=recent_count,
        recency_ratio=(recent_count / active_count) if active_count else 0.0,
        class_c_total=class_c_total,
        class_c_recent=class_c_recent,
        class_c_open=class_c_open,
        class_c_rate=(class_c_total / active_count) if active_count else 0.0,
        non_compliance_total=non_compliance_total,
        non_compliance_recent=non_compliance_recent,
        accepted_cert=accepted_cert,
        rejected_cert=rejected_cert,
        cert_acceptance_rate=(accepted_cert / cert_attempts) if cert_attempts else None,
        max_days_overdue=max_days_overdue,
        max_years_overdue=max_days_overdue / 365,
        top_sig_notices=top_sig_notices,
        top_sig_span_years=top_sig_span,
        top_sig_breadth=top_sig_breadth,
        top_sig_coherent=top_sig_coherent,
        top_sig_ordernumber=top_sig_ordernumber,
        n_persistent_sigs=n_persistent,
        n_chronic_sigs=n_chronic,
        n_defect_visits=n_defect_visits,
        defect_visit_span_years=defect_visit_span_years,
        frozen_overdue_count=frozen_overdue_count,
        frozen_overdue_share=(frozen_overdue_count / real_defect_count) if real_defect_count else 0.0,
        frozen_years_max=frozen_years_max,
        stale_status_count=stale_status_count,
        timeline_fields_present=timeline_present,
    )
    p.scale = _level_scale(p.active_count)
    p.recency = _level_recency(p.recency_ratio)
    p.severity = _level_severity(p.class_c_rate, p.class_c_open)
    p.engagement = _level_engagement(p.accepted_cert, p.rejected_cert, p.max_days_overdue)
    p.pattern = _level_pattern(p.n_persistent_sigs, p.n_chronic_sigs, p.real_defect_count)
    p.backlog_age = _level_backlog(p.max_days_overdue)
    # Independent of pattern (recurrence): true when nobody has ever engaged
    # with the violation, nothing's happened lately, and it's been overdue
    # for 9.7+ years. Deliberately not folded into `pattern` - it can be true
    # alongside Chronic/Persistent just as easily as Isolated (the same
    # recurring defect can also be one nobody's ever certified or revisited).
    p.long_unresolved = (
        p.recency == "Gone quiet"
        and p.engagement in ("Unaddressed", "Too early to tell")
        and p.backlog_age in ("Long overdue", "Decades overdue")
    )
    return p


def generate_narrative(p: BuildingProfile) -> str:
    """Assemble an evidence-based sentence from the six dimension values.
    Every clause traces to a specific field on the profile — no characterization
    of the building or owner, only what the records show."""
    parts = []

    # Scale + recency + severity opener
    if p.recency == "Active surge":
        if p.active_count == 1:
            opener = "The one violation on file was issued in the past two years"
        else:
            if p.recency_ratio >= 0.98:
                recency_phrase = "all issued in the past two years"
            elif p.recency_ratio >= 0.90:
                recency_phrase = f"nearly all ({p.recent_count} of {p.active_count}) issued in the past two years"
            else:
                recency_phrase = f"the large majority ({p.recent_count} of {p.active_count}) issued in the past two years"
            opener = f"A wave of {p.active_count} violations, {recency_phrase}"
    elif p.recency == "Gone quiet":
        opener = f"{p.active_count} open violation{'s' if p.active_count != 1 else ''}, with little to no activity in the past two years"
    else:
        opener = f"{p.active_count} open violations, {p.recent_count} of them issued in the past two years"
    if p.class_c_total > 0:
        opener += f", {p.class_c_total} of them serious (Class C)"
    parts.append(opener + ".")

    if p.pattern == "No real defects":
        parts.append(
            "None of these are physical defect records; every one is an administrative "
            "filing requirement (such as registration or bedbug-report compliance), not a "
            "cited problem with the building itself."
        )
    elif p.pattern == "Isolated" and p.real_defect_count < p.active_count:
        # active_count includes administrative filings (registration lapses,
        # bedbug reports); the Isolated label is decided on real_defect_count
        # alone. Without this, a building can show e.g. "11 open violations"
        # and "Isolated" with nothing explaining why 11 didn't earn Widespread -
        # the same kind of hidden-number confusion this whole taxonomy pass
        # was meant to remove, just from the administrative side instead of
        # the volume side.
        admin_count = p.active_count - p.real_defect_count
        parts.append(
            f"{admin_count} of these are administrative filings, and only "
            f"{p.real_defect_count} real physical defects are on record."
        )

    # A building can be Isolated/Widespread (no single defect ever recurred)
    # and still keep getting hit with a *different* real problem at nearly
    # every inspection - the signature-recurrence check above can't see
    # that, since it only tracks the same ordernumber repeating. Widespread
    # additionally earns a volume comparison ("more than most buildings ever
    # see") - true by construction, since Widespread only fires at/above the
    # calibrated p75 cutoff (REAL_DEFECT_WIDESPREAD_THRESHOLD). That claim
    # would be FALSE for an Isolated building, which sits at or below that
    # same cutoff, so it's deliberately never made there.
    has_visit_detail = p.n_defect_visits >= 3 and p.defect_visit_span_years >= 0.5
    if p.pattern == "Widespread":
        if has_visit_detail:
            parts.append(
                f"None of these problems have repeated, but different issues have been cited "
                f"across {p.n_defect_visits} separate inspections over {p.defect_visit_span_years:.1f} "
                "years, more than most buildings ever see."
            )
        else:
            parts.append(
                f"None of these problems have repeated, but {p.real_defect_count} separate real "
                "defects are on record here, more than most buildings ever see."
            )
    elif p.pattern == "Isolated" and has_visit_detail:
        parts.append(
            f"Different problems have been cited across {p.n_defect_visits} separate inspections "
            f"over {p.defect_visit_span_years:.1f} years, even though no single defect has recurred."
        )

    # Pattern
    if p.pattern in ("Chronic", "Persistent"):
        if p.top_sig_breadth >= 2:
            where = f"across {p.top_sig_breadth} different apartments"
        elif p.top_sig_breadth == 1:
            where = "in the same apartment"
        else:
            where = "in the building's common areas"
        if p.top_sig_coherent:
            what = "The same specific problem"
        else:
            what = "The same administrative code, covering several different underlying defects,"
        parts.append(
            f"{what} has recurred {p.top_sig_notices} times over "
            f"{p.top_sig_span_years:.1f} years, {where}."
        )

    # Engagement
    if p.engagement == "Unaddressed":
        if p.non_compliance_total > 0:
            parts.append("The correction deadlines have passed with no owner response: no certification has ever been filed, and some violations are flagged non-compliant.")
        else:
            parts.append("The correction deadlines have passed and no certification has ever been filed for any of these violations.")
    elif p.engagement == "Too early to tell":
        if p.accepted_cert + p.rejected_cert > 0:
            parts.append("Only one or two certifications are on record, too few to read the owner's pattern.")
        else:
            parts.append("No certification has been attempted yet for these violations.")
    elif p.engagement == "Responsive":
        parts.append(f"Every certification attempt on record has been accepted ({p.accepted_cert} of {p.accepted_cert + p.rejected_cert}).")
    elif p.engagement == "Resistant":
        parts.append(f"Certification attempts have mostly been rejected ({p.rejected_cert} of {p.accepted_cert + p.rejected_cert} on record).")
    elif p.engagement == "Mixed engagement":
        parts.append(f"Certification attempts have had mixed outcomes ({p.accepted_cert} accepted, {p.rejected_cert} rejected).")

    # Backlog age. Deliberately one sentence regardless of long_unresolved -
    # that flag requires Gone-quiet recency and Unaddressed engagement by
    # definition, both of which the opener and Engagement sentence above
    # have *already* stated by the time this runs, in whatever wording their
    # own branch used. A long_unresolved-specific variant here inevitably
    # re-says one of those facts in different words no matter how it's
    # phrased - that's what kept resurfacing as "yet another" duplicate.
    # Deadline-age and total-silence are different clocks (see
    # _frozen_silent_years()) and can point at different violations, so
    # they're stated as separate facts rather than one number qualifying
    # the other.
    if p.backlog_age in ("Long overdue", "Decades overdue"):
        parts.append(f"The oldest open violation here is {p.max_years_overdue:.1f} years past its correction deadline.")
    if p.timeline_fields_present and p.frozen_overdue_count >= 2:
        parts.append(
            f"In addition, {p.frozen_overdue_count} violations have sat frozen with no recorded "
            f"activity in over two years: no filing from the owner, no follow-up from the city. "
            f"The most neglected of these has had no update in {int(p.frozen_years_max)} years."
        )

    return " ".join(parts)
