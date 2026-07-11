"""Rule regression tests for the database rules AZ-DB-001 .. AZ-DB-004."""

import scanner.rules.az_db_001 as az_db_001
import scanner.rules.az_db_002 as az_db_002
import scanner.rules.az_db_003 as az_db_003
import scanner.rules.az_db_004 as az_db_004
from tests.helpers.mock_azure import make_resource

_REQUIRED_FIELDS = {
    "rule_id",
    "rule_name",
    "severity",
    "category",
    "resource_id",
    "resource_name",
    "resource_type",
    "description",
    "remediation",
    "playbook",
    "frameworks",
    "metadata",
}

_SUB = "00000000-0000-0000-0000-000000000001"
_RG = "rg-test"


def _sql_id(name):
    return f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/Microsoft.Sql/servers/{name}"


def _firewall_rule(name, start_ip, end_ip):
    return make_resource(
        name=name,
        start_ip_address=start_ip,
        end_ip_address=end_ip,
    )


def test_db_004_compliant_returns_no_findings(mock_azure, subscription_id):
    """A SQL Server with no AllowAzureServices rule must produce no findings."""
    server = make_resource(id=_sql_id("sql-restricted"), name="sql-restricted")
    rule = _firewall_rule("AllowSpecificIP", "203.0.113.10", "203.0.113.10")
    mock_azure.set_sql_servers([server])
    mock_azure.set_sql_server_firewall_rules(_RG, "sql-restricted", [rule])
    findings = az_db_004.scan(mock_azure, subscription_id)
    assert findings == []


def test_db_004_noncompliant_returns_one_finding(mock_azure, subscription_id):
    """A SQL Server with AllowAllWindowsAzureIps rule must produce exactly one finding."""
    server = make_resource(id=_sql_id("sql-open"), name="sql-open")
    allow_azure = _firewall_rule("AllowAllWindowsAzureIps", "0.0.0.0", "0.0.0.0")
    mock_azure.set_sql_servers([server])
    mock_azure.set_sql_server_firewall_rules(_RG, "sql-open", [allow_azure])
    findings = az_db_004.scan(mock_azure, subscription_id)
    assert len(findings) == 1
    finding = findings[0]
    assert _REQUIRED_FIELDS.issubset(finding.keys())
    assert finding["rule_id"] == "AZ-DB-004"
    assert finding["severity"] == "HIGH"
    assert finding["category"] == "Database"
    assert finding["resource_name"] == "sql-open"
    assert finding["metadata"]["resource_group"] == _RG


def test_db_004_no_firewall_rules_returns_no_findings(mock_azure, subscription_id):
    """A SQL Server with no firewall rules must produce no findings."""
    server = make_resource(id=_sql_id("sql-no-rules"), name="sql-no-rules")
    mock_azure.set_sql_servers([server])
    mock_azure.set_sql_server_firewall_rules(_RG, "sql-no-rules", [])
    findings = az_db_004.scan(mock_azure, subscription_id)
    assert findings == []


def _pg_id(name, provider="Microsoft.DBforPostgreSQL/servers"):
    return f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/{provider}/{name}"


# ── AZ-DB-001: PostgreSQL single-server public network access ───────────────


def test_db_001_compliant_returns_no_findings(mock_azure, subscription_id):
    server = make_resource(
        id=_pg_id("pg-private"),
        name="pg-private",
        public_network_access="Disabled",
        location="eastus",
    )
    mock_azure.set_postgresql_servers([server])
    assert az_db_001.scan(mock_azure, subscription_id) == []


def test_db_001_noncompliant_returns_one_finding(mock_azure, subscription_id):
    server = make_resource(
        id=_pg_id("pg-public"),
        name="pg-public",
        public_network_access="Enabled",
        location="eastus",
    )
    mock_azure.set_postgresql_servers([server])
    findings = az_db_001.scan(mock_azure, subscription_id)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "AZ-DB-001"
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["resource_name"] == "pg-public"


# ── AZ-DB-002: Azure SQL server auditing disabled ───────────────────────────


def test_db_002_compliant_returns_no_findings(mock_azure, subscription_id):
    server = make_resource(id=_sql_id("sql-audited"), name="sql-audited")
    mock_azure.set_sql_servers([server])
    mock_azure.set_sql_server_auditing_policy(_RG, "sql-audited", make_resource(state="Enabled"))
    assert az_db_002.scan(mock_azure, subscription_id) == []


def test_db_002_noncompliant_returns_one_finding(mock_azure, subscription_id):
    server = make_resource(id=_sql_id("sql-unaudited"), name="sql-unaudited")
    mock_azure.set_sql_servers([server])
    mock_azure.set_sql_server_auditing_policy(_RG, "sql-unaudited", make_resource(state="Disabled"))
    findings = az_db_002.scan(mock_azure, subscription_id)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "AZ-DB-002"
    assert findings[0]["severity"] == "MEDIUM"
    assert findings[0]["resource_name"] == "sql-unaudited"


# ── AZ-DB-003: PostgreSQL flexible server SSL enforcement disabled ──────────


def test_db_003_compliant_returns_no_findings(mock_azure, subscription_id):
    server = make_resource(
        id=_pg_id("pgflex-ssl", "Microsoft.DBforPostgreSQL/flexibleServers"),
        name="pgflex-ssl",
        location="eastus",
    )
    mock_azure.set_postgresql_flexible_servers([server])
    mock_azure.set_postgresql_flexible_server_parameters(
        _RG,
        "pgflex-ssl",
        [make_resource(name="require_secure_transport", value="on")],
    )
    assert az_db_003.scan(mock_azure, subscription_id) == []


def test_db_003_noncompliant_returns_one_finding(mock_azure, subscription_id):
    server = make_resource(
        id=_pg_id("pgflex-nossl", "Microsoft.DBforPostgreSQL/flexibleServers"),
        name="pgflex-nossl",
        location="eastus",
    )
    mock_azure.set_postgresql_flexible_servers([server])
    mock_azure.set_postgresql_flexible_server_parameters(
        _RG,
        "pgflex-nossl",
        [make_resource(name="require_secure_transport", value="off")],
    )
    findings = az_db_003.scan(mock_azure, subscription_id)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "AZ-DB-003"
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["resource_name"] == "pgflex-nossl"
