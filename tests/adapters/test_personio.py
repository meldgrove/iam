import json
from datetime import date, timedelta

import httpx
import pytest
from django.utils import timezone

from iam.adapters.base import AdapterError, ConfigurationError, TransientError
from iam.adapters.personio import PersonioAdapter

CONFIG = {"client_id": "client", "client_secret": "s3cret-value"}


def attr(value, label=None):
    data = {"value": value}
    if label:
        data["label"] = label
    return data


def employee(pid, first, last, **attributes):
    return {
        "type": "Employee",
        "attributes": {
            "id": attr(pid),
            "first_name": attr(first, "First name"),
            "last_name": attr(last, "Last name"),
            **{key: attr(value) for key, value in attributes.items()},
        },
    }


def personio(pages, attributes=None, auth_status=200):
    """A fake Personio API: auth, attribute catalog, employee pages by offset."""
    attributes = attributes or []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth":
            if auth_status != 200:
                return httpx.Response(auth_status, json={"success": False})
            body = json.loads(request.content)
            assert body == {"client_id": "client", "client_secret": "s3cret-value"}
            return httpx.Response(200, json={"success": True, "data": {"token": "tok"}})
        assert request.headers["Authorization"] == "Bearer tok"
        if request.url.path == "/v1/company/employees/attributes":
            return httpx.Response(200, json={"success": True, "data": attributes})
        if request.url.path == "/v1/company/employees":
            offset = int(request.url.params["offset"])
            index = offset // 100
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "metadata": {"current_page": index, "total_pages": len(pages)},
                    "data": pages[index],
                },
            )
        return httpx.Response(404)

    return handler


def adapter(mock_transport, handler, **config):
    transport = mock_transport(handler)
    return PersonioAdapter({**CONFIG, **config}, transport=transport), transport


def test_pages_through_employees_and_maps_fields(mock_transport):
    boss = employee(
        "1",
        "Ada",
        "Boss",
        email="ada@example.invalid",
        status="active",
        hire_date="2020-01-01T00:00:00+01:00",
        department={"type": "Department", "attributes": {"id": 7, "name": "Engineering"}},
        employment_type="internal",
    )
    filler = [employee(str(n), "Filler", f"P{n}", status="active") for n in range(100, 199)]
    worker = employee(
        "2",
        "Bo",
        "Worker",
        email="bo@example.invalid",
        status="active",
        position="Werkstudent",
        employment_type="internal",
        hire_date="01.03.2027",
        department={"type": "Department", "attributes": {"id": 7, "name": "Engineering"}},
        supervisor={"type": "Employee", "attributes": {"id": attr(1)}},
    )
    adapter_, transport = adapter(
        mock_transport,
        personio([[boss, *filler], [worker]]),
        type_mapping="werkstudent = intern\nnonsense = astronaut",
    )

    people = {record.external_id: record for record in adapter_.list_people()}

    assert len(people) == 101
    offsets = [
        r.url.params["offset"] for r in transport.requests if r.url.path == "/v1/company/employees"
    ]
    assert offsets == ["0", "100"]
    assert people["1"].start_date == date(2020, 1, 1)
    assert people["1"].team == "Engineering"
    assert people["1"].person_type == "employee"
    bo = people["2"]
    assert (bo.first_name, bo.last_name, bo.work_email) == ("Bo", "Worker", "bo@example.invalid")
    assert bo.person_type == "intern"
    assert bo.job_title == "Werkstudent"
    assert bo.start_date == date(2027, 3, 1)
    # Supervisor given only by Personio ID resolves to that person's work email.
    assert bo.manager_email == "ada@example.invalid"
    assert bo.active is True
    # The token is fetched once.
    assert sum(r.url.path == "/v1/auth" for r in transport.requests) == 1


def test_discovers_custom_attributes_by_label(mock_transport):
    attributes = [
        {"key": "dynamic_1", "label": "Private E-Mail-Adresse", "type": "standard"},
        {"key": "dynamic_2", "label": "Workplaces Supervisor", "type": "standard"},
        {"key": "dynamic_3", "label": "Favorite color", "type": "standard"},
        {"key": "dynamic_4", "label": "Cost center", "type": "standard"},
    ]
    record = employee("5", "Cy", "Custom", status="active", email="cy@example.invalid")
    record["attributes"]["dynamic_1"] = {
        "label": "Private E-Mail-Adresse",
        "value": "cy@home.example.invalid",
    }
    record["attributes"]["dynamic_2"] = {
        "label": "Workplaces Supervisor",
        "value": {
            "type": "Employee",
            "attributes": {"id": attr(9), "email": attr("lead@example.invalid")},
        },
    }
    record["attributes"]["dynamic_4"] = {"label": "Cost center", "value": "Ops"}
    adapter_, transport = adapter(
        mock_transport, personio([[record]], attributes), team_attribute="Cost center"
    )

    [cy] = adapter_.list_people()

    listed = [r for r in transport.requests if r.url.path == "/v1/company/employees"][0]
    keys = listed.url.params.get_list("attributes[]")
    assert "dynamic_1" in keys and "dynamic_2" in keys and "dynamic_4" in keys
    assert "dynamic_3" not in keys
    assert cy.personal_email == "cy@home.example.invalid"
    assert cy.manager_email == "lead@example.invalid"
    assert cy.team == "Ops"


def test_end_dates_and_inactive_window(mock_transport):
    today = timezone.localdate()
    future = (today + timedelta(days=20)).isoformat()
    recent = (today - timedelta(days=5)).isoformat()
    old = (today - timedelta(days=90)).isoformat()
    records = [
        # Termination date wins over the contract end date.
        employee(
            "10", "Fu", "Ture", status="active", termination_date=future, contract_end_date=old
        ),
        # Left recently: listed as inactive.
        employee("11", "Re", "Cent", status="inactive", termination_date=recent),
        # Left long ago: not listed.
        employee("12", "Ol", "D", status="inactive", termination_date=old),
        # Inactive without a date: listed, so the app sees the change.
        employee("13", "No", "Date", status="terminated"),
        # The last day at work never ends access.
        employee("14", "La", "St", status="active", last_day_at_work=recent),
        # Past contract end without a status change: inactive.
        employee("15", "Co", "Ntract", status="active", end_date=recent),
    ]
    adapter_, _ = adapter(mock_transport, personio([records]))

    people = {r.external_id: r for r in adapter_.list_people()}

    assert set(people) == {"10", "11", "13", "14", "15"}
    assert people["10"].end_date == today + timedelta(days=20)
    assert people["10"].active is True
    assert people["11"].active is False
    assert people["13"].active is False and people["13"].end_date is None
    assert people["14"].active is True and people["14"].end_date is None
    assert people["15"].active is False


def test_skips_unreadable_records_and_keeps_the_rest(mock_transport):
    records = [
        employee("20", "Bad", "Date", status="active", hire_date="next tuesday"),
        employee("21", "", "Nameless", status="active"),
        employee("22", "Good", "Record", status="active", employment_type="external"),
    ]
    adapter_, _ = adapter(mock_transport, personio([records]))

    people = adapter_.list_people()

    assert [p.external_id for p in people] == ["22"]
    assert people[0].person_type == "external"
    assert [record_id for record_id, _ in adapter_.skipped] == ["20", "21"]


def test_work_email_domains_move_other_addresses_to_personal(mock_transport):
    records = [
        employee("30", "Wo", "Rk", status="active", email="wo@corp.example.invalid"),
        employee("31", "Pr", "Ivate", status="active", email="pr@mail.example.invalid"),
    ]
    adapter_, _ = adapter(
        mock_transport, personio([records]), work_email_domains="corp.example.invalid"
    )

    people = {r.external_id: r for r in adapter_.list_people()}

    assert people["30"].work_email == "wo@corp.example.invalid"
    assert people["31"].work_email == ""
    assert people["31"].personal_email == "pr@mail.example.invalid"


def test_rejected_credentials_are_a_configuration_error(mock_transport):
    adapter_, _ = adapter(mock_transport, personio([[]], auth_status=401))

    with pytest.raises(ConfigurationError) as error:
        adapter_.test_connection()

    assert "s3cret-value" not in str(error.value)


def test_rate_limit_is_transient(mock_transport):
    def handler(request):
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        return httpx.Response(429)

    adapter_, _ = adapter(mock_transport, handler)

    with pytest.raises(TransientError):
        list(adapter_.list_people())


def test_unexpected_answer_is_an_adapter_error(mock_transport):
    def handler(request):
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        return httpx.Response(200, json={"data": {"not": "a list"}})

    adapter_, _ = adapter(mock_transport, handler)

    with pytest.raises(AdapterError):
        adapter_.list_people()


def test_missing_credentials_are_reported_before_any_call(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(500))
    adapter_ = PersonioAdapter({"client_id": "client"}, transport=transport)

    with pytest.raises(ConfigurationError, match="Client secret"):
        adapter_.test_connection()
    assert transport.requests == []


def test_connection_test_reports_employee_count(mock_transport):
    adapter_, _ = adapter(mock_transport, personio([[employee("40", "On", "E", status="active")]]))

    result = adapter_.test_connection()

    assert result.ok
    assert result.message == "Connected to Personio: 1 employee."


def test_the_configured_team_attribute_comes_first(mock_transport):
    page = [
        employee("1", "Ada", "Lovelace", department="Engineering", team="Operations"),
        employee("2", "Grace", "Hopper", department=None, team="Research"),
    ]
    personio_adapter, _ = adapter(mock_transport, personio([page]), team_attribute="team")
    teams = {r.external_id: r.team for r in personio_adapter.list_people()}
    assert teams == {"1": "Operations", "2": "Research"}
    personio_adapter, _ = adapter(mock_transport, personio([page]))
    teams = {r.external_id: r.team for r in personio_adapter.list_people()}
    assert teams == {"1": "Engineering", "2": "Research"}
