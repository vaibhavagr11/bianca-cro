from app.resolution import Evidence, decide, external_addresses, resolve, rules_company

NAMES = {"acme": "Acme Foods", "globex": "Globex"}
INTERNAL, PROJECT = {"bianca-ai.com"}, {"kellogg.northwestern.edu"}


def ev(company, *emails, confident=True):
    return Evidence(record_id=str(id(emails)), company_id=company, confident=confident,
                    addresses={e: e.split("@")[0] for e in emails})


def test_external_addresses_excludes_bianca_project_and_rooms():
    participants = {
        "from": [{"address": "Jane@Acme.com", "name": "Jane"}],
        "to": [{"address": "ramiro.godfrid@bianca-ai.com"}],
        "cc": [{"address": "eren@kellogg.northwestern.edu"}],
        "attendees": [{"address": "room@resource.calendar.google.com"}],
    }
    assert external_addresses(participants, INTERNAL, PROJECT) == {"jane@acme.com": "Jane"}


def test_domain_with_company_name_is_trusted_from_one_record():
    res = resolve([ev("acme", "jane@acmefoods.com")], NAMES)
    assert res.domains["acmefoods.com"].trusted


def test_other_domain_needs_three_unanimous_records():
    two = resolve([ev("globex", "a@gx.io"), ev("globex", "b@gx.io")], NAMES)
    three = resolve([ev("globex", "a@gx.io"), ev("globex", "b@gx.io"), ev("globex", "c@gx.io")], NAMES)
    assert not two.domains["gx.io"].trusted
    assert three.domains["gx.io"].trusted


def test_partner_domain_seen_with_two_customers_is_not_mapped():
    evidence = [ev("acme", "x@vendor.com"), ev("acme", "y@vendor.com"), ev("globex", "z@vendor.com")]
    assert "vendor.com" not in resolve(evidence, NAMES).domains


def test_freemail_never_becomes_a_company_domain_but_contact_is_mapped():
    res = resolve([ev("acme", "gus@gmail.com")], NAMES)
    assert "gmail.com" not in res.domains
    assert res.contact_company["gus@gmail.com"] == "acme"


def test_low_confidence_records_are_not_evidence():
    res = resolve([ev("acme", "jane@acmefoods.com", confident=False)], NAMES)
    assert res.domains == {} and res.contact_company["jane@acmefoods.com"] is None


def test_rules_company_unique_or_none():
    res = resolve([ev("acme", "jane@acmefoods.com"), ev("globex", "bob@globex.com")], NAMES)
    assert rules_company({"new.person@acmefoods.com": ""}, res) == "acme"
    assert rules_company({"jane@acmefoods.com": "", "bob@globex.com": ""}, res) is None
    assert rules_company({"someone@unknown.com": ""}, res) is None


def test_decide():
    assert decide("acme", "acme") == ("acme", "both")
    assert decide("acme", "globex") == ("acme", "conflict")
    assert decide(None, "globex") == ("globex", "rules")
    assert decide("acme", None) == ("acme", "classifier")
    assert decide(None, None) == (None, None)
